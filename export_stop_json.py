"""
Genera un archivo JSON por cada parada a partir de metrobus.sqlite,
listo para servirse como API estática con GitHub Pages.

Muestra en 'lines' únicamente los códigos/números de línea (ej. ["L150", "L160"]).

Uso:
  pip install --break-system-packages
  python export_stop_json.py
"""

import json
import shutil
import sqlite3
from pathlib import Path

DB_PATH = Path("./metrobus.sqlite")
OUTPUT_DIR = Path("./docs/stops")
INDEX_PATH = Path("./docs/stops_index.json")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"No se encuentra {DB_PATH} — ejecuta antes gtfs_to_sqlite.py")

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    (OUTPUT_DIR.parent / ".nojekyll").touch()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stops = conn.execute("""
        SELECT stop_id, stop_name, stop_lat, stop_lon
        FROM stops
    """).fetchall()

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stops_cols = {row[1] for row in conn.execute("PRAGMA table_info(stops)")} if "stops" in tables else set()
    trips_cols = {row[1] for row in conn.execute("PRAGMA table_info(trips)")} if "trips" in tables else set()
    routes_cols = {row[1] for row in conn.execute("PRAGMA table_info(routes)")} if "routes" in tables else set()
    agency_cols = {row[1] for row in conn.execute("PRAGMA table_info(agency)")} if "agency" in tables else set()
    calendar_cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar)")} if "calendar" in tables else set()

    has_headsign = "trip_headsign" in trips_cols
    has_calendar = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}.issubset(calendar_cols)
    has_parent_station = "parent_station" in stops_cols

    route_short_expr = "r.route_short_name" if "route_short_name" in routes_cols else "r.route_id"
    route_long_expr = "r.route_long_name" if "route_long_name" in routes_cols else "NULL"
    route_color_expr = "r.route_color" if "route_color" in routes_cols else "NULL"
    agency_name_expr = "a.agency_name" if "agency_name" in agency_cols else "NULL"

    # 1. Mapa de paradas hijo a estación padre (si la columna parent_station existe)
    parent_map = {}
    if has_parent_station:
        parent_rows = conn.execute(
            "SELECT stop_id, parent_station FROM stops WHERE parent_station IS NOT NULL AND parent_station != ''"
        ).fetchall()
        for pr in parent_rows:
            parent_map[str(pr["stop_id"]).strip()] = str(pr["parent_station"]).strip()

    # 2. Consultar números/códigos de línea por cada parada
    lines_rows = conn.execute(f"""
        SELECT DISTINCT
            st.stop_id,
            COALESCE(NULLIF({route_short_expr}, ''), {route_long_expr}, r.route_id) AS route_code
        FROM stop_times st
        JOIN trips t  ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
    """).fetchall()

    lines_by_stop = {}
    for row in lines_rows:
        s_id = str(row["stop_id"]).strip()
        line_code = (row["route_code"] or "").strip()
        if line_code:
            lines_by_stop.setdefault(s_id, set()).add(line_code)
            if s_id in parent_map:
                p_id = parent_map[s_id]
                lines_by_stop.setdefault(p_id, set()).add(line_code)

    for s_id, lines_set in lines_by_stop.items():
        lines_by_stop[s_id] = sorted(list(lines_set))

    # 3. Determinar destino (headsign)
    if not has_headsign:
        conn.execute("""
            CREATE TEMP TABLE IF NOT EXISTS trip_last_stop AS
            SELECT st.trip_id, s.stop_name AS last_stop_name
            FROM stop_times st
            JOIN stops s ON s.stop_id = st.stop_id
            WHERE st.stop_sequence = (
                SELECT MAX(CAST(st2.stop_sequence AS INTEGER))
                FROM stop_times st2
                WHERE st2.trip_id = st.trip_id
            )
        """)
        headsign_candidates = []
        if "trip_headsign" in trips_cols:
            headsign_candidates.append("t.trip_headsign")
        if "route_long_name" in routes_cols:
            headsign_candidates.append("r.route_long_name")
        headsign_candidates.append("tls.last_stop_name")
        trip_headsign_expr = f"COALESCE({', '.join(headsign_candidates)})"
    else:
        trip_headsign_expr = "t.trip_headsign"

    calendar_select = """
        COALESCE(c.monday, 1) AS monday,
        COALESCE(c.tuesday, 1) AS tuesday,
        COALESCE(c.wednesday, 1) AS wednesday,
        COALESCE(c.thursday, 1) AS thursday,
        COALESCE(c.friday, 1) AS friday,
        COALESCE(c.saturday, 1) AS saturday,
        COALESCE(c.sunday, 1) AS sunday
    """ if has_calendar else """
        1 AS monday, 1 AS tuesday, 1 AS wednesday, 1 AS thursday, 1 AS friday, 1 AS saturday, 1 AS sunday
    """

    calendar_join = "LEFT JOIN calendar c ON c.service_id = t.service_id" if has_calendar else ""
    where_clause = "WHERE st.stop_id = ? OR st.stop_id IN (SELECT stop_id FROM stops WHERE parent_station = ?)" if has_parent_station else "WHERE st.stop_id = ?"

    departures_sql = f"""
        SELECT DISTINCT
            st.departure_time,
            st.stop_sequence,
            t.trip_id,
            {trip_headsign_expr} AS trip_headsign,
            t.service_id,
            r.route_id,
            COALESCE(NULLIF({route_short_expr}, ''), {route_long_expr}, r.route_id) AS route_short_name,
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
        {where_clause}
        ORDER BY st.departure_time
    """

    print(f"Generando JSON para {len(stops)} paradas…")

    stops_index = []

    for stop in stops:
        stop_id = str(stop["stop_id"]).strip()

        params = (stop_id, stop_id) if has_parent_station else (stop_id,)
        departures = conn.execute(departures_sql, params).fetchall()
        lines = lines_by_stop.get(stop_id, [])

        stop_json = {
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "stop_lat": float(stop["stop_lat"]) if stop["stop_lat"] else 0.0,
            "stop_lon": float(stop["stop_lon"]) if stop["stop_lon"] else 0.0,
            "lines": lines,
            "departures": [
                {
                    "departure_time": d["departure_time"],
                    "trip_id": str(d["trip_id"]),
                    "headsign": d["trip_headsign"] or "",
                    "service_id": str(d["service_id"]),
                    "route_id": str(d["route_id"]),
                    "route_short_name": d["route_short_name"] or "",
                    "route_long_name": d["route_long_name"] or "",
                    "route_color": d["route_color"] or "",
                    "agency_name": d["agency_name"] or "",
                    "days": {
                        "monday": int(d["monday"]),
                        "tuesday": int(d["tuesday"]),
                        "wednesday": int(d["wednesday"]),
                        "thursday": int(d["thursday"]),
                        "friday": int(d["friday"]),
                        "saturday": int(d["saturday"]),
                        "sunday": int(d["sunday"]),
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
            "stop_lat": float(stop["stop_lat"]) if stop["stop_lat"] else 0.0,
            "stop_lon": float(stop["stop_lon"]) if stop["stop_lon"] else 0.0,
            "lines": lines,
        })

    INDEX_PATH.write_text(
        json.dumps(stops_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    conn.close()

    total_size = sum(f.stat().st_size for f in OUTPUT_DIR.glob("*.json"))
    print(f"Listo → {len(stops)} archivos en {OUTPUT_DIR} ({total_size / 1024:.0f} KB en total)")
    print(f"Índice → {INDEX_PATH} ({INDEX_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
