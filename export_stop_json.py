"""
Genera un archivo JSON por cada parada a partir de metrobus.sqlite
de forma rápida e indexada.

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

    # 1. Crear tabla temporal ultrarrápida con el nombre de la última parada de cada trip (como destino)
    conn.execute("""
        CREATE TEMP TABLE trip_last_stop AS
        SELECT trip_id, stop_name AS last_stop_name
        FROM (
            SELECT
                st.trip_id,
                s.stop_name,
                ROW_NUMBER() OVER (
                    PARTITION BY st.trip_id
                    ORDER BY CAST(st.stop_sequence AS INTEGER) DESC
                ) AS rn
            FROM stop_times st
            JOIN stops s ON s.stop_id = st.stop_id
        )
        WHERE rn = 1;
    """)

    # 2. Consultar únicamente los códigos de línea por cada parada
    lines_rows = conn.execute("""
        SELECT DISTINCT
            st.stop_id,
            COALESCE(NULLIF(r.route_short_name, ''), r.route_id) AS route_code
        FROM stop_times st
        JOIN trips t  ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
    """).fetchall()

    lines_by_stop = {}
    for row in lines_rows:
        s_id = str(row["stop_id"])
        line_code = (row["route_code"] or "").strip()
        if line_code:
            lines_by_stop.setdefault(s_id, set()).add(line_code)

    for s_id, lines_set in lines_by_stop.items():
        lines_by_stop[s_id] = sorted(list(lines_set))

    # 3. Consulta optimizada para extraer las salidas de cada parada
    departures_sql = """
        SELECT DISTINCT
            COALESCE(NULLIF(st.departure_time, ''), NULLIF(st.arrival_time, ''), '00:00:00') AS departure_time,
            st.stop_sequence,
            st.trip_id,
            COALESCE(NULLIF(r.route_long_name, ''), tls.last_stop_name, r.route_short_name, '') AS trip_headsign,
            t.service_id,
            r.route_id,
            COALESCE(NULLIF(r.route_short_name, ''), r.route_id) AS route_short_name,
            COALESCE(r.route_long_name, '') AS route_long_name,
            '' AS route_color,
            COALESCE(a.agency_name, '') AS agency_name,
            COALESCE(c.monday, 1) AS monday,
            COALESCE(c.tuesday, 1) AS tuesday,
            COALESCE(c.wednesday, 1) AS wednesday,
            COALESCE(c.thursday, 1) AS thursday,
            COALESCE(c.friday, 1) AS friday,
            COALESCE(c.saturday, 1) AS saturday,
            COALESCE(c.sunday, 1) AS sunday
        FROM stop_times st
        JOIN trips t        ON t.trip_id = st.trip_id
        JOIN routes r       ON r.route_id = t.route_id
        LEFT JOIN agency a  ON a.agency_id = r.agency_id
        LEFT JOIN calendar c ON c.service_id = t.service_id
        LEFT JOIN trip_last_stop tls ON tls.trip_id = t.trip_id
        WHERE st.stop_id = ?
        ORDER BY departure_time
    """

    print(f"Generando JSON para {len(stops)} paradas…")

    stops_index = []

    for stop in stops:
        stop_id = str(stop["stop_id"]).strip()

        departures = conn.execute(departures_sql, (stop_id,)).fetchall()
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
                    "trip_id": str(d["trip_id"] or ""),
                    "headsign": d["trip_headsign"] or "",
                    "service_id": str(d["service_id"] or ""),
                    "route_id": str(d["route_id"] or ""),
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
