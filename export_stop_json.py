"""
Genera un archivo JSON por cada parada a partir de metrobus.sqlite,
listo para servirse como API estática con GitHub Pages.

Cada archivo docs/stops/<stop_id>.json contiene la información de la
parada y todas sus salidas programadas (horario GTFS estático), agrupadas
por servicio (service_id) para que el cliente pueda filtrar por día.

No calcula "próximas salidas en vivo" — eso lo hace la app en el cliente,
comparando la hora actual con departure_time. Este JSON es el horario
completo de esa parada, no un cálculo dependiente del momento de
generación (así el mismo archivo sirve todo el día sin regenerarse).

Uso:
  pip install --break-system-packages   # no requiere librerías extra
  python export_stop_json.py
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path("./metrobus.sqlite")
OUTPUT_DIR = Path("./docs/stops")
INDEX_PATH = Path("./docs/stops_index.json")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"No se encuentra {DB_PATH} — ejecuta antes gtfs_to_sqlite.py")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Evita que GitHub Pages procese docs/ con Jekyll (que fallaría al
    # intentar aplicar un tema por defecto sobre archivos JSON planos).
    (OUTPUT_DIR.parent / ".nojekyll").touch()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stops = conn.execute("""
        SELECT stop_id, stop_name, stop_lat, stop_lon
        FROM stops
    """).fetchall()

    # Algunas columnas de GTFS son opcionales (p.ej. trip_headsign,
    # route_color) y este feed concreto puede no traerlas. Comprobamos
    # qué columnas existen realmente antes de construir la consulta,
    # para no romper si faltan.
    trips_cols = {row[1] for row in conn.execute("PRAGMA table_info(trips)")}
    routes_cols = {row[1] for row in conn.execute("PRAGMA table_info(routes)")}
    agency_cols = {row[1] for row in conn.execute("PRAGMA table_info(agency)")}

    trip_headsign_expr = "t.trip_headsign" if "trip_headsign" in trips_cols else "NULL"
    route_short_expr = "r.route_short_name" if "route_short_name" in routes_cols else "NULL"
    route_long_expr = "r.route_long_name" if "route_long_name" in routes_cols else "NULL"
    route_color_expr = "r.route_color" if "route_color" in routes_cols else "NULL"
    agency_name_expr = "a.agency_name" if "agency_name" in agency_cols else "NULL"
    has_headsign = "trip_headsign" in trips_cols

    if not has_headsign:
        print("Aviso: el GTFS no trae trip_headsign — se usará "
              "route_long_name (o, si tampoco existe, la última parada "
              "del trayecto) como destino.")
        # última parada de cada trip (mayor stop_sequence), como último
        # recurso si tampoco hay route_long_name
        conn.execute("""
            CREATE TEMP TABLE trip_last_stop AS
            SELECT st.trip_id, s.stop_name AS last_stop_name
            FROM stop_times st
            JOIN stops s ON s.stop_id = st.stop_id
            WHERE st.stop_sequence = (
                SELECT MAX(st2.stop_sequence)
                FROM stop_times st2
                WHERE st2.trip_id = st.trip_id
            )
        """)
        # Prioridad: trip_headsign (si existiera) > route_long_name > última parada
        headsign_candidates = []
        if "trip_headsign" in trips_cols:
            headsign_candidates.append("t.trip_headsign")
        if "route_long_name" in routes_cols:
            headsign_candidates.append("r.route_long_name")
        headsign_candidates.append("tls.last_stop_name")
        trip_headsign_expr = f"COALESCE({', '.join(headsign_candidates)})"

    departures_sql = f"""
        SELECT
            st.departure_time,
            st.stop_sequence,
            t.trip_id,
            {trip_headsign_expr} AS trip_headsign,
            t.service_id,
            r.route_id,
            {route_short_expr} AS route_short_name,
            {route_long_expr} AS route_long_name,
            {route_color_expr} AS route_color,
            {agency_name_expr} AS agency_name
        FROM stop_times st
        JOIN trips t   ON t.trip_id = st.trip_id
        JOIN routes r  ON r.route_id = t.route_id
        LEFT JOIN agency a ON a.agency_id = r.agency_id
        {"LEFT JOIN trip_last_stop tls ON tls.trip_id = t.trip_id" if not has_headsign else ""}
        WHERE st.stop_id = ?
        ORDER BY st.departure_time
    """

    print(f"Generando JSON para {len(stops)} paradas…")

    # Líneas (route_short_name) que pasan por cada parada, en una sola
    # consulta agregada — evita una query extra por cada una de las
    # ~2000 paradas.
    lines_by_stop = {}
    lines_rows = conn.execute(f"""
        SELECT DISTINCT
            st.stop_id,
            {route_short_expr} AS route_short_name,
            r.route_id,
            {route_color_expr} AS route_color
        FROM stop_times st
        JOIN trips t  ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
    """).fetchall()
    for row in lines_rows:
        lines_by_stop.setdefault(row["stop_id"], []).append({
            "route_id": row["route_id"],
            "route_short_name": row["route_short_name"],
            "route_color": row["route_color"],
        })
    # orden estable por nombre de línea dentro de cada parada
    for stop_id, lines in lines_by_stop.items():
        lines.sort(key=lambda l: (l["route_short_name"] or ""))

    stops_index = []

    for stop in stops:
        stop_id = stop["stop_id"]

        departures = conn.execute(departures_sql, (stop_id,)).fetchall()
        lines = lines_by_stop.get(stop_id, [])

        stop_json = {
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "stop_lat": stop["stop_lat"],
            "stop_lon": stop["stop_lon"],
            "lines": lines,
            "departures": [
                {
                    "departure_time": d["departure_time"],
                    "trip_id": d["trip_id"],
                    "headsign": d["trip_headsign"],
                    "service_id": d["service_id"],
                    "route_id": d["route_id"],
                    "route_short_name": d["route_short_name"],
                    "route_long_name": d["route_long_name"],
                    "route_color": d["route_color"],
                    "agency_name": d["agency_name"],
                }
                for d in departures
            ],
        }

        out_path = OUTPUT_DIR / f"{stop_id}.json"
        out_path.write_text(
            json.dumps(stop_json, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        stops_index.append({
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "stop_lat": stop["stop_lat"],
            "stop_lon": stop["stop_lon"],
            "lines": lines,
        })

    # índice ligero: para que la app pueda buscar/listar paradas sin
    # tener que descargar los JSON individuales
    INDEX_PATH.write_text(
        json.dumps(stops_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    conn.close()

    total_size = sum(f.stat().st_size for f in OUTPUT_DIR.glob("*.json"))
    print(f"Listo → {len(stops)} archivos en {OUTPUT_DIR} "
          f"({total_size / 1024:.0f} KB en total)")
    print(f"Índice → {INDEX_PATH} ({INDEX_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
