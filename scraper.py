import asyncio
import aiohttp
import json
import os

URL_STOPS_BASE = "https://api.softoursistemas.com/metrobus/old/stops"
URL_STOP_ROUTES = "https://api.softoursistemas.com/metrobus/stops/code/{stop_code}/routes"
URL_ROUTE_SHAPES = "https://api.softoursistemas.com/metrobus/routes/{route_id}/shapes"

CONCURRENCIA = 15

def encode_polyline(coordinates):
    """
    Convierte coordenadas GeoJSON [[lon, lat], ...] al formato estándar Encoded Polyline.
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

# -------------------------------------------------------------------------
# FASE 1: Obtener parada limpia y registrar metadatos de líneas
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
# FASE 2: Descargar trazado (shape) por dirección
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
# Flujo Principal
# -------------------------------------------------------------------------
async def main():
    sem = asyncio.Semaphore(CONCURRENCIA)
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # Crear carpetas de destino
    os.makedirs("data/shapes", exist_ok=True)

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Descargar catálogo base de paradas
        print("1. Descargando paradas base...")
        async with session.get(URL_STOPS_BASE) as resp:
            raw_stops = await resp.json()

        # 2. Consultar líneas de cada parada
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

        # -----------------------------------------------------------------
        # ARCHIVO 1: data/metrobus_stops.json
        # -----------------------------------------------------------------
        with open("data/metrobus_stops.json", "w", encoding="utf-8") as f:
            json.dump({
                "_descripcion_campos": {
                    "stop_id": "Identificador numérico único de la parada en Metrobús.",
                    "stop_name": "Nombre o denominación oficial de la parada.",
                    "stop_lat": "Coordenada de latitud WGS84 en grados decimales.",
                    "stop_lon": "Coordenada de longitud WGS84 en grados decimales.",
                    "lines": "Array con los códigos comerciales de las líneas que prestan servicio.",
                    "parent_station": "(Opcional) Identificador del intercambiador o nodo padre si existe."
                },
                "stops": paradas_finales
            }, f, ensure_ascii=False, indent=2)

        # -----------------------------------------------------------------
        # ARCHIVO 2: data/metrobus_lines.json
        # -----------------------------------------------------------------
        lineas_ordenadas = [catalogo_lineas[k] for k in sorted(catalogo_lineas.keys())]
        with open("data/metrobus_lines.json", "w", encoding="utf-8") as f:
            json.dump({
                "_descripcion_campos": {
                    "line_code": "Código comercial de la línea para matching en la app (ej. 170B).",
                    "route_id": "Identificador interno de la ruta en la API de Softour.",
                    "concesion": "Código oficial de la concesión administrativa (ej. CV102, CV106).",
                    "route_short_name": "Nombre corto oficial de la ruta.",
                    "route_long_name": "Descripción completa de la ruta y cabeceras de origen/destino."
                },
                "total_lines": len(lineas_ordenadas),
                "lines": lineas_ordenadas
            }, f, ensure_ascii=False, indent=2)

        print(f"Generados metrobus_stops.json ({len(paradas_finales)} paradas) y metrobus_lines.json ({len(lineas_ordenadas)} líneas).")

        # -----------------------------------------------------------------
        # ARCHIVO 3: Carpeta data/shapes/{line_code}.json
        # -----------------------------------------------------------------
        print("3. Extrayendo polylines para cada línea (sentidos 0 y 1)...")
        
        for linea in lineas_ordenadas:
            code = linea["line_code"]
            r_id = linea["route_id"]
            conc = linea["concesion"]

            if not r_id or not conc:
                continue

            # Consultar direcciones 0 y 1 concurrentemente para esta línea
            shape_tasks = [
                fetch_shape_direction(session, sem, r_id, conc, 0),
                fetch_shape_direction(session, sem, r_id, conc, 1)
            ]
            results = await asyncio.gather(*shape_tasks)

            line_shapes = {}
            for direction, encoded_str in results:
                if encoded_str:
                    line_shapes[str(direction)] = encoded_str

            # Solo creamos el archivo si se obtuvo al menos un trazado
            if line_shapes:
                ruta_archivo = f"data/shapes/{code}.json"
                with open(ruta_archivo, "w", encoding="utf-8") as f:
                    json.dump(line_shapes, f, ensure_ascii=False, indent=2)

        print("¡Completado! Todas las geometrías han sido procesadas en data/shapes/.")

if __name__ == "__main__":
    asyncio.run(main())
