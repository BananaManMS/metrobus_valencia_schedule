"""
Descarga el GTFS de transporte interurbano de la Generalitat Valenciana,
carga todos los calendarios, rutas y paradas, y genera metrobus.sqlite
de forma completa, incluyendo TODAS las paradas registradas.

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

# Bounding box amplio para abarcar toda la red interurbana sin recortar zonas limítrofes
LAT_MIN, LAT_MAX = 37.50, 41.00
LON_MIN, LON_MAX = -2.50, 1.00

REQUIRED_FILES = [
    "agency.txt",
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
    if name in zf.namelist():
        with zf.open(name) as f:
            return pd.read_csv(f, dtype=str, keep_default_na=False)
    return pd.DataFrame()


def main():
    zf = download_gtfs()

    missing = [f for f in REQUIRED_FILES if f not in zf.namelist()]
    if missing:
        raise SystemExit(f"Faltan archivos esenciales en el GTFS descargado: {missing}")

    print("Cargando GTFS…")
    agency = load(zf, "agency.txt")
    routes = load(zf, "routes.txt")
    trips = load(zf, "trips.txt")
    stop_times = load(zf, "stop_times.txt")
    stops = load(zf, "stops.txt")
    calendar = load(zf, "calendar.txt")
    calendar_dates = load(zf, "calendar_dates.txt")

    # --- 1. Validar coordenadas de paradas ---
    stops["stop_lat_num"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon_num"] = pd.to_numeric(stops["stop_lon"], errors="coerce")

    # Mantenemos las paradas dentro del rango geográfico regional
    stops_in_box = stops[
        stops["stop_lat_num"].between(LAT_MIN, LAT_MAX)
        & stops["stop_lon_num"].between(LON_MIN, LON_MAX)
    ].copy()
    stops_in_box.drop(columns=["stop_lat_num", "stop_lon_num"], inplace=True)

    print(f"Paradas en el GTFS original: {len(stops)} → válidas: {len(stops_in_box)}")

    # --- 2. MANTENER TODAS LAS PARADAS VÁLIDAS ---
    # Incluye todas las paradas en SQLite (tengan o no salidas asociadas)
    stops_f = stops_in_box.copy()

    # --- 3. Filtrar expediciones, rutas, agencias y calendarios ---
    valid_stop_ids = set(stops_f["stop_id"])
    stop_times_f = stop_times[stop_times["stop_id"].isin(valid_stop_ids)].copy()

    valid_trip_ids = set(stop_times_f["trip_id"])
    trips_f = trips[trips["trip_id"].isin(valid_trip_ids)].copy()

    valid_route_ids = set(trips_f["route_id"])
    routes_f = routes[routes["route_id"].isin(valid_route_ids)].copy()

    valid_agency_ids = set(routes_f["agency_id"]) if "agency_id" in routes_f.columns else set()
    agency_f = agency[agency["agency_id"].isin(valid_agency_ids)].copy() if valid_agency_ids else agency.copy()

    valid_service_ids = set(trips_f["service_id"])
    calendar_f = calendar[calendar["service_id"].isin(valid_service_ids)].copy() if not calendar.empty else calendar
    calendar_dates_f = calendar_dates[calendar_dates["service_id"].isin(valid_service_ids)].copy() if not calendar_dates.empty else calendar_dates

    # --- 4. Volcar a SQLite ---
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(OUTPUT_DB)

    agency_f.to_sql("agency", conn, index=False)
    routes_f.to_sql("routes", conn, index=False)
    trips_f.to_sql("trips", conn, index=False)
    stops_f.to_sql("stops", conn, index=False)
    stop_times_f.to_sql("stop_times", conn, index=False)

    if not calendar_f.empty:
        calendar_f.to_sql("calendar", conn, index=False)
    if not calendar_dates_f.empty:
        calendar_dates_f.to_sql("calendar_dates", conn, index=False)

    # Crear índices para acelerar consultas
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_stop_times_stop_id ON stop_times(stop_id);",
        "CREATE INDEX IF NOT EXISTS idx_stop_times_trip_id ON stop_times(trip_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_route_id ON trips(route_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_service_id ON trips(service_id);",
        "CREATE INDEX IF NOT EXISTS idx_routes_agency_id ON routes(agency_id);",
    ]

    if not calendar_f.empty:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_calendar_service_id ON calendar(service_id);")
    if not calendar_dates_f.empty:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_calendar_dates_service_id ON calendar_dates(service_id);")

    conn.executescript("\n".join(indexes))
    conn.commit()
    conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 * 1024)
    print(f"\n¡Base de datos generada con éxito! → {OUTPUT_DB} ({size_mb:.1f} MB)")
    print(f"Total paradas procesadas e importadas: {len(stops_f)}")


if __name__ == "__main__":
    main()
