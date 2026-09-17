import asyncio
import aiohttp
import json
import os
import hashlib

URL_STOPS_BASE = "https://api.softoursistemas.com/metrobus/old/stops"
URL_STOP_ROUTES = "https://api.softoursistemas.com/metrobus/stops/code/{stop_code}/routes"
URL_ROUTE_SHAPES = "https://api.softoursistemas.com/metrobus/routes/{route_id}/shapes"

CONCURRENCIA = 15

def encode_polyline(coordinates):
    """
    Convierte una lista GeoJSON [[lon, lat], ...] al formato estándar Encoded Polyline.
    """
    if not coordinates:
        return ""
    result = []
    prev_lat, prev_lon = 0, 0
    for pt in coordinates:
        lon, lat = pt[0], pt[1]
        lat_int = round(lat * 1e5)
        lon_int = round(lon * 1e5)
        d_lat = lat_int - prev_lat
        d_lon = lon_int - prev_lon
        prev_lat, prev_lon = lat_int, lon_int
        for delta in (d_lat, d_lon):
            delta = ~(delta << 1) if delta < 0 else (delta << 1)
            while delta >= 0x20:
                result.append(chr((0x20 | (delta & 0x1f)) + 63))
                delta >>= 5
            result.append(chr(delta + 63))
    return "".join(result)

def guardar_json_si_cambia(ruta_archivo, nuevo_contenido):
    """
    Compara el hash SHA-256 del contenido generado con el existente en disco.
    Solo escribe en el sistema de archivos si hay cambios reales en los datos.
    Devuelve True si se actualizó, False si era idéntico.
    """
    nuevo_texto = json.dumps(nuevo_contenido, ensure_ascii=False, indent=2, sort_keys=True)
    nuevo_bytes = nuevo_texto.encode("utf-8")
    nuevo_hash = hashlib.sha256(nuevo_bytes).hexdigest()

    if os.path.exists(ruta_archivo):
        with open(ruta_archivo, "rb") as f:
            existente_hash = hashlib.sha256(f.read()).hexdigest()
        if nuevo_hash == existente_hash:
            return False

    os.makedirs(os.path.dirname(ruta_archivo), exist_ok=True)
    with open(ruta_archivo, "wb") as f:
        f.write(nuevo_bytes)
    return True

# -------------------------------------------------------------------------
# FASE 1: Obtener paradas y censo de líneas
# -------------------------------------------------------------------------
async def fetch_stop_and_lines(session, sem, raw_stop):
    stop_id = str(raw_stop.get("stop_id") or raw_stop.get("stop_code") or raw_stop.get("_id") or "").strip()
    url = URL_STOP_ROUTES.format(stop_code=stop_id)
    
    line_codes = []
    lines_metadata = []

    async with sem:
        for intento in range(3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for r in data:
                            short_name = r.get("route_short_name")
                            if short_name:
                                s_name = short_name.strip()
                                line_codes.append(s_name)
                                lines_metadata.append({
                                    "line_code": s_name,
                                    "route_id": str(r.get("route_id", "")).strip(),
                                    "concesion": str(r.get("concesion", "")).strip(),
                                    "route_short_name": s_name,
                                    "route_long_name": str(r.get("route_long_name", "")).strip()
                                })
                        break
                    elif resp.status == 429:
                        await asyncio.sleep(1.5 * (intento + 1))
            except Exception:
                await asyncio.sleep(0.5)

    stop_clean = {
        "stop_id": stop_id,
        "stop_name": raw_stop.get("stop_name", "").strip(),
        "stop_lat": float(raw_stop.get("stop_lat")) if raw_stop.get("stop_lat") is not None else None,
        "stop_lon": float(raw_stop.get("stop_lon")) if raw_stop.get("stop_lon") is not None else None,
        "lines": sorted(list(set(line_codes)))
    }
    
    parent = raw_stop.get("parent_station")
    if parent and str(parent).strip():
        stop_clean["parent_station"] = str(parent).strip()

    return stop_clean, lines_metadata

# -------------------------------------------------------------------------
# FASE 2: Descargar polilínea por dirección (0 y 1)
# -------------------------------------------------------------------------
async def fetch_shape_direction(session, sem, route_id, concesion, direction):
    url = URL_ROUTE_SHAPES.format(route_id=route_id)
    params = {"direction": direction, "concesion": concesion}
    
    async with sem:
        for intento in range(3):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        features = data.get("features", [])
                        if features:
                            coords = features[0].get("geometry", {}).get("coordinates", [])
                            return direction, encode_polyline(coords)
            except Exception:
                await asyncio.sleep(0.5)
    return direction, ""

# -------------------------------------------------------------------------
# Orquestador Principal
# -------------------------------------------------------------------------
async def main():
    sem = asyncio.Semaphore(CONCURRENCIA)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    os.makedirs("data", exist_ok=True)

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Descargar catálogo base de paradas
        print("1. Descargando paradas base...")
        async with session.get(URL_STOPS_BASE) as resp:
            raw_stops = await resp.json()

        # 2. Consultar líneas activas para cada parada
        print(f"2. Escaneando {len(raw_stops)} paradas...")
        stop_tasks = [fetch_stop_and_lines(session, sem, s) for s in raw_stops]
        stops_results = await asyncio.gather(*stop_tasks)

        paradas_finales = []
        catalogo_lineas = {}

        for p_clean, metadatos in stops_results:
            paradas_finales.append(p_clean)
            for m in metadatos:
                code = m["line_code"]
                if code not in catalogo_lineas:
                    catalogo_lineas[code] = m

        # Orden determinista para paradas
        paradas_finales.sort(key=lambda x: int(x["stop_id"]) if x["stop_id"].isdigit() else x["stop_id"])

        # -----------------------------------------------------------------
        # ARCHIVO 1: data/metrobus_stops.json
        # -----------------------------------------------------------------
        doc_stops = {
            "_descripcion_campos": {
                "stop_id": "Identificador numérico único de la parada en Metrobús.",
                "stop_name": "Nombre oficial de la parada.",
                "stop_lat": "Latitud WGS84.",
                "stop_lon": "Longitud WGS84.",
                "lines": "Códigos de líneas comerciales que operan en la parada.",
                "parent_station": "(Opcional) Identificador del nodo/estación nodriza padre."
            },
            "stops": paradas_finales
        }
        stops_actualizado = guardar_json_si_cambia("data/metrobus_stops.json", doc_stops)
        estado_stops = "ACTUALIZADO" if stops_actualizado else "SIN CAMBIOS (omitido)"
        print(f"-> data/metrobus_stops.json: {estado_stops}")

        # -----------------------------------------------------------------
        # ARCHIVO 2: data/metrobus_lines.json
        # -----------------------------------------------------------------
        lineas_ordenadas = [catalogo_lineas[k] for k in sorted(catalogo_lineas.keys())]
        doc_lines = {
            "_descripcion_campos": {
                "line_code": "Código comercial de la línea para matching en la app (ej. 170B).",
                "route_id": "Identificador interno de la ruta en la API de Softour.",
                "concesion": "Código oficial de la concesión administrativa (ej. CV102, CV106).",
                "route_short_name": "Nombre corto oficial de la ruta.",
                "route_long_name": "Descripción completa de la ruta e itinerario."
            },
            "total_lines": len(lineas_ordenadas),
            "lines": lineas_ordenadas
        }
        lines_actualizado = guardar_json_si_cambia("data/metrobus_lines.json", doc_lines)
        estado_lines = "ACTUALIZADO" if lines_actualizado else "SIN CAMBIOS (omitido)"
        print(f"-> data/metrobus_lines.json: {estado_lines}")

        # -----------------------------------------------------------------
        # ARCHIVO 3: data/metrobus_shapes.json (Consolidado único)
        # -----------------------------------------------------------------
        print("3. Extrayendo y consolidando geometrías por línea...")
        todos_los_shapes = {}

        for linea in lineas_ordenadas:
            code = linea["line_code"]
            r_id = linea["route_id"]
            conc = linea["concesion"]

            if not r_id or not conc:
                continue

            # Consultar sentidos 0 y 1 concurrentemente para esta línea
            shape_tasks = [
                fetch_shape_direction(session, sem, r_id, conc, 0),
                fetch_shape_direction(session, sem, r_id, conc, 1)
            ]
            results = await asyncio.gather(*shape_tasks)

            line_shapes = {}
            for direction, encoded_str in results:
                if encoded_str:
                    line_shapes[str(direction)] = encoded_str

            if line_shapes:
                todos_los_shapes[code] = line_shapes

        shapes_actualizado = guardar_json_si_cambia("data/metrobus_shapes.json", todos_los_shapes)
        estado_shapes = "ACTUALIZADO" if shapes_actualizado else "SIN CAMBIOS (omitido)"
        print(f"-> data/metrobus_shapes.json: {estado_shapes} ({len(todos_los_shapes)} líneas incluidas)")

        print("\nEjecución finalizada con éxito.")

if __name__ == "__main__":
    asyncio.run(main())
