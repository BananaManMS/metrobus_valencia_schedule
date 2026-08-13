"""
Genera un archivo JSON por cada parada a partir de metrobus.sqlite,
listo para servirse como API estática con GitHub Pages.

Limpia automáticamente la carpeta docs/stops antes de cada ejecución
para eliminar paradas obsoletas o huérfanas.
"""

import json
import shutil
import sqlite3
from pathlib import Path

DB_PATH = Path("./metrobus.sqlite")
OUTPUT_DIR = Path("./docs/stops")
INDEX_PATH = Path("./docs/stops_index.json")
NOJEKYLL_PATH = Path("./docs/.nojekyll")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"No se encuentra {DB_PATH} — ejecuta antes gtfs_to_sqlite.py")

    # 1. Elimina la carpeta docs/stops si existe para borrar archivos antiguos/obsoletos
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Asegura la creación de .nojekyll para GitHub Pages
    if not NOJEKYLL_PATH.exists():
        NOJEKYLL_PATH.touch()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stops = conn.execute("""
        SELECT stop_id, stop_name, stop_lat, stop_lon
        FROM stops
    """).fetchall()

    # Comprobaciones de tablas y columnas en SQLite
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    trips_cols = {row[1] for row in conn.execute("PRAGMA table_info(trips)")} if "trips" in tables else set()
    routes_cols = {row[1] for row in conn.execute("PRAGMA table_info(routes)")} if "routes" in tables else set()
    agency_cols = {row[1] for row in conn.execute("PRAGMA table_info(agency)")} if "agency" in tables else set()
    calendar_cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar)")} if "calendar" in tables else set()

    has_headsign = "trip_headsign" in trips_cols
    has_calendar = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}.issubset(calendar_cols)

    trip_headsign_expr = "t.trip_headsign" if "trip_headsign" in trips_cols else "NULL"
    route_short_expr = "r.route_short_name" if "route_short_name" in routes_cols else "NULL"
    route_long_expr = "r.route_long_name" if "route_long_name" in routes_cols else "NULL"
    route_color_expr = "r.route_color" if "route_color" in routes_cols else "NULL"
    agency_name_expr = "a.agency_name" if "agency_name" in agency_cols else "NULL"

    if not has_headsign:
        print("Aviso: el GTFS no trae trip_headsign — se usará el nombre "
              "de la última parada de cada trip como destino.")
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
        trip_headsign_expr = "COALESCE(t.trip_headsign, tls.last_stop_name)" \
            if "trip_headsign" in trips_cols else "tls.last_stop_name"

    calendar_select = """
        c.monday, c.tuesday, c.wednesday, c.thursday, c.friday, c.saturday, c.sunday
    """ if has_calendar else """
        1 AS monday, 1 AS tuesday, 1 AS wednesday, 1 AS thursday, 1 AS friday, 1 AS saturday, 1 AS sunday
    """

    calendar_join = "LEFT JOIN calendar c ON c.service_id = t.service_id" if has_calendar else ""

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
            {agency_name_expr} AS agency_name,
            {calendar_select}
        FROM stop_times st
        JOIN trips t   ON t.trip_id = st.trip_id
        JOIN routes r  ON r.route_id = t.route_id
        LEFT JOIN agency a ON a.agency_id = r.agency_id
        {calendar_join}
        {"LEFT JOIN trip_last_stop tls ON tls.trip_id = t.trip_id" if not has_headsign else ""}
        WHERE st.stop_id = ?
        ORDER BY st.departure_time
    """

    print(f"Generando JSON para {len(stops)} paradas…")

    stops_index = []

    for stop in stops:
        stop_id = stop["stop_id"]

        departures = conn.execute(departures_sql, (stop_id,)).fetchall()

        stop_json = {
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "stop_lat": stop["stop_lat"],
            "stop_lon": stop["stop_lon"],
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
                    "days": {
                        "monday": d["monday"] if d["monday"] is not None else 0,
                        "tuesday": d["tuesday"] if d["tuesday"] is not None else 0,
                        "wednesday": d["wednesday"] if d["wednesday"] is not None else 0,
                        "thursday": d["thursday"] if d["thursday"] is not None else 0,
                        "friday": d["friday"] if d["friday"] is not None else 0,
                        "saturday": d["saturday"] if d["saturday"] is not None else 0,
                        "sunday": d["sunday"] if d["sunday"] is not None else 0,
                    }
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
        })

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
