"""
Descarga el GTFS de transporte interurbano de la Generalitat Valenciana,
lo filtra a la provincia de Valencia mediante bounding box, y genera un
SQLite.

shapes.txt se ignora deliberadamente (geometría de mapa, no la usa la app
y es la mayor parte del peso del feed) — ni se descarga su contenido al
DataFrame en memoria más de lo necesario.

Uso:
  pip install pandas requests --break-system-packages
  python gtfs_to_sqlite.py
"""

import io
import sqlite3
import zipfile
from pathlib import Path

import pandas as pd
import requests

GTFS_URL = (
    "https://dadesobertes.gva.es/dataset/2f380ffd-b389-4ff4-9f7c-be92b30fbf28"
    "/resource/3c8a2e6b-5b5e-49f5-872f-5f33fcd52547/download/gtfs.zip"
)
OUTPUT_DB = Path("./metrobus.sqlite")

# Bounding box aproximado de la provincia de Valencia (WGS84)
LAT_MIN, LAT_MAX = 38.75, 39.90
LON_MIN, LON_MAX = -1.60, -0.05

REQUIRED_FILES = [
    "agency.txt",
    "calendar_dates.txt",
    "routes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
]


def download_gtfs() -> zipfile.ZipFile:
    print(f"Descargando GTFS desde {GTFS_URL} …")
    resp = requests.get(GTFS_URL, timeout=120)
    resp.raise_for_status()
    print(f"Descargado ({len(resp.content) / (1024 * 1024):.1f} MB)")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def load(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
    with zf.open(name) as f:
        return pd.read_csv(f, dtype=str, keep_default_na=False)


def main():
    zf = download_gtfs()

    missing = [f for f in REQUIRED_FILES if f not in zf.namelist()]
    if missing:
        raise SystemExit(f"Faltan archivos en el GTFS descargado: {missing}")

    print("Cargando GTFS…")
    agency = load(zf, "agency.txt")
    routes = load(zf, "routes.txt")
    trips = load(zf, "trips.txt")
    stop_times = load(zf, "stop_times.txt")
    stops = load(zf, "stops.txt")
    calendar_dates = load(zf, "calendar_dates.txt")

    # --- 1. Filtrar paradas dentro del bounding box de la provincia ---
    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")

    stops_in_box = stops[
        stops["stop_lat"].between(LAT_MIN, LAT_MAX)
        & stops["stop_lon"].between(LON_MIN, LON_MAX)
    ].copy()
    print(f"Paradas totales: {len(stops)} → dentro del bbox: {len(stops_in_box)}")

    valid_stop_ids = set(stops_in_box["stop_id"])

    # --- 2. Filtrar stop_times a esas paradas ---
    stop_times_f = stop_times[stop_times["stop_id"].isin(valid_stop_ids)].copy()
    print(f"stop_times totales: {len(stop_times)} → filtrados: {len(stop_times_f)}")

    # --- 3. Filtrar trips que tengan al menos una parada dentro del bbox ---
    valid_trip_ids = set(stop_times_f["trip_id"])
    trips_f = trips[trips["trip_id"].isin(valid_trip_ids)].copy()
    print(f"trips totales: {len(trips)} → filtrados: {len(trips_f)}")

    # --- 4. Filtrar routes usadas por esos trips ---
    valid_route_ids = set(trips_f["route_id"])
    routes_f = routes[routes["route_id"].isin(valid_route_ids)].copy()
    print(f"routes totales: {len(routes)} → filtradas: {len(routes_f)}")

    # --- 5. Filtrar agency usada por esas routes ---
    valid_agency_ids = set(routes_f["agency_id"]) if "agency_id" in routes_f.columns else set()
    agency_f = agency[agency["agency_id"].isin(valid_agency_ids)].copy() if valid_agency_ids else agency.copy()
    print(f"agencies totales: {len(agency)} → filtradas: {len(agency_f)}")

    # --- 6. Re-filtrar stop_times a solo los trips finales (por si algún  ---
    #        trip quedó fuera al filtrar routes/agency)
    stop_times_f = stop_times_f[stop_times_f["trip_id"].isin(trips_f["trip_id"])]

    # --- 7. Re-filtrar stops a los realmente usados en stop_times final ---
    final_stop_ids = set(stop_times_f["stop_id"])
    stops_f = stops_in_box[stops_in_box["stop_id"].isin(final_stop_ids)].copy()

    # --- 8. calendar_dates: solo servicios usados por los trips filtrados ---
    valid_service_ids = set(trips_f["service_id"])
    calendar_dates_f = calendar_dates[calendar_dates["service_id"].isin(valid_service_ids)].copy()

    # --- 9. Volcar a SQLite ---
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(OUTPUT_DB)

    agency_f.to_sql("agency", conn, index=False)
    routes_f.to_sql("routes", conn, index=False)
    trips_f.to_sql("trips", conn, index=False)
    stops_f.to_sql("stops", conn, index=False)
    stop_times_f.to_sql("stop_times", conn, index=False)
    calendar_dates_f.to_sql("calendar_dates", conn, index=False)

    # Índices para consultas rápidas ("próximas salidas por parada")
    conn.executescript("""
        CREATE INDEX idx_stop_times_stop_id ON stop_times(stop_id);
        CREATE INDEX idx_stop_times_trip_id ON stop_times(trip_id);
        CREATE INDEX idx_trips_route_id ON trips(route_id);
        CREATE INDEX idx_trips_service_id ON trips(service_id);
        CREATE INDEX idx_routes_agency_id ON routes(agency_id);
        CREATE INDEX idx_calendar_dates_service_id ON calendar_dates(service_id);
    """)
    conn.commit()
    conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 * 1024)
    print(f"\nListo → {OUTPUT_DB} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
