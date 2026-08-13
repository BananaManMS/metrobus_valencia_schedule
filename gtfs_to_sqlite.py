"""
Descarga el GTFS de la Generalitat Valenciana, filtra las agencias de Metrobús,
sintetiza 'calendar' desde 'calendar_dates' y genera metrobus.sqlite.

A prueba de columnas opcionales ausentes (como parent_station).

Uso:
  pip install pandas requests --break-system-packages
  python gtfs_to_sqlite.py
"""

import datetime
import io
import sqlite3
import time
import unicodedata
import zipfile
from pathlib import Path

import pandas as pd
import requests

GTFS_URL = (
    "https://dadesobertes.gva.es/dataset/2f380ffd-b389-4ff4-9f7c-be92b30fbf28"
    "/resource/3c8a2e6b-5b5e-49f5-872f-5f33fcd52547/download/gtfs.zip"
)
FALLBACK_ZIP = Path("./gtfs_fallback.zip")
OUTPUT_DB = Path("./metrobus.sqlite")

METROBUS_AGENCIES = {
    "VALENCIA METROPOLITANA NORD",
    "VALENCIA METROPOLITANA NORD-OEST",
    "VALENCIA METROPOLITANA OEST",
    "VALENCIA METROPOLITANA SUD",
    "LA HOYA DE BUÑOL - VALENCIA",
    "MONTSERRAT - VALENCIA",
    "LA SERRANIA - VALENCIA",
    "ALTO PALANCIA - SAGUNT - VALENCIA",
    "VALENCIA - BENIFAIO",
    "LA RIBERA - VALENCIA",
}

REQUIRED_FILES = [
    "agency.txt",
    "routes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
]


def remove_accents(input_str: str) -> str:
    if not input_str:
        return ""
    nfkd_form = unicodedata.normalize('NFKD', str(input_str))
    only_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    return only_ascii.upper().strip()


def download_gtfs() -> zipfile.ZipFile:
    print(f"Intentando descargar GTFS desde {GTFS_URL} …")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for intento in range(1, 4):
        try:
            resp = requests.get(GTFS_URL, headers=headers, timeout=60)
            resp.raise_for_status()
            print(f"Descargado con éxito ({len(resp.content) / (1024 * 1024):.1f} MB)")
            return zipfile.ZipFile(io.BytesIO(resp.content))
        except Exception as e:
            print(f"⚠️ Intento {intento} fallido ({e}). Reintentando en 5s…")
            time.sleep(5)

    if FALLBACK_ZIP.exists():
        print(f"📁 Usando archivo de respaldo local: {FALLBACK_ZIP}")
        return zipfile.ZipFile(FALLBACK_ZIP)
    
    raise SystemExit("❌ Error crítico: No se pudo descargar el GTFS y no existe gtfs_fallback.zip")


def load(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
    if name in zf.namelist():
        with zf.open(name) as f:
            df = pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            for col in df.columns:
                df[col] = df[col].str.strip()
            return df
    return pd.DataFrame()


def main():
    zf = download_gtfs()

    missing = [f for f in REQUIRED_FILES if f not in zf.namelist()]
    if missing:
        raise SystemExit(f"Faltan archivos esenciales en el GTFS descargado: {missing}")

    print("Cargando datos del GTFS…")
    agency = load(zf, "agency.txt")
    routes = load(zf, "routes.txt")
    trips = load(zf, "trips.txt")
    stop_times = load(zf, "stop_times.txt")
    stops = load(zf, "stops.txt")
    calendar_dates = load(zf, "calendar_dates.txt")

    # 1. Filtrar agencias de Metrobús
    agency["agency_norm"] = agency["agency_name"].apply(remove_accents)
    agency_f = agency[agency["agency_norm"].isin(METROBUS_AGENCIES)].copy()
    agency_f.drop(columns=["agency_norm"], inplace=True)

    valid_agency_ids = set(agency_f["agency_id"]) if "agency_id" in agency_f.columns else set()
    print(f"Agencias seleccionadas: {len(agency_f)}")

    # 2. Filtrar rutas
    if "agency_id" in routes.columns:
        routes_f = routes[routes["agency_id"].isin(valid_agency_ids) | (routes["agency_id"] == "")].copy()
    else:
        routes_f = routes.copy()

    valid_route_ids = set(routes_f["route_id"])
    print(f"Rutas Metrobús conservadas: {len(routes_f)}")

    # 3. Filtrar trips
    trips_f = trips[trips["route_id"].isin(valid_route_ids)].copy()
    valid_trip_ids = set(trips_f["trip_id"])
    print(f"Trips asociados: {len(trips_f)}")

    # 4. Filtrar stop_times
    stop_times_f = stop_times[stop_times["trip_id"].isin(valid_trip_ids)].copy()
    valid_stop_ids = set(stop_times_f["stop_id"])

    # 5. Filtrar stops e incluir paradas padre solo si la columna existe
    if "parent_station" in stops.columns:
        parents = set(stops[stops["stop_id"].isin(valid_stop_ids)]["parent_station"])
        parents.discard("")
        valid_stop_ids.update(parents)

    stops_f = stops[stops["stop_id"].isin(valid_stop_ids)].copy()
    print(f"Paradas finales de Metrobús: {len(stops_f)}")

    # 6. SINTETIZAR CALENDAR DESDE CALENDAR_DATES
    valid_service_ids = set(trips_f["service_id"])
    calendar_dates_f = calendar_dates[calendar_dates["service_id"].isin(valid_service_ids)].copy() if not calendar_dates.empty else calendar_dates

    print("Sintetizando la tabla 'calendar' desde las fechas de 'calendar_dates'…")
    service_days = {}
    for _, row in calendar_dates_f.iterrows():
        if str(row.get("exception_type")).strip() == "1":
            sid = row["service_id"]
            dt_str = row["date"]
            try:
                dt = datetime.datetime.strptime(dt_str, "%Y%m%d")
                day_idx = dt.weekday()
                if sid not in service_days:
                    service_days[sid] = [0] * 7
                service_days[sid][day_idx] = 1
            except Exception:
                pass

    calendar_rows = []
    for sid, days in service_days.items():
        calendar_rows.append({
            "service_id": sid,
            "monday": days[0],
            "tuesday": days[1],
            "wednesday": days[2],
            "thursday": days[3],
            "friday": days[4],
            "saturday": days[5],
            "sunday": days[6]
        })

    calendar_f = pd.DataFrame(calendar_rows)
    print(f"Tabla 'calendar' sintetizada con éxito ({len(calendar_f)} servicios).")

    # 7. Volcar a SQLite
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

    # Crear índices de forma dinámica e insensibles a columnas opcionales
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_stop_times_stop_id ON stop_times(stop_id);",
        "CREATE INDEX IF NOT EXISTS idx_stop_times_trip_id ON stop_times(trip_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_route_id ON trips(route_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_service_id ON trips(service_id);",
        "CREATE INDEX IF NOT EXISTS idx_routes_agency_id ON routes(agency_id);",
    ]

    if "parent_station" in stops_f.columns:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_stops_parent ON stops(parent_station);")

    if not calendar_f.empty:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_calendar_service_id ON calendar(service_id);")

    conn.executescript("\n".join(indexes))
    conn.commit()
    conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 * 1024)
    print(f"\n¡Base de datos limpia generada con éxito! → {OUTPUT_DB} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
