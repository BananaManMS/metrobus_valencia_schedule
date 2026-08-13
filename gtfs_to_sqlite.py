"""
Descarga el GTFS de transporte interurbano de la Generalitat Valenciana,
filtra únicamente los operadores del Área Metropolitana de Valencia (Metrobús),
sintetiza la tabla 'calendar' desde 'calendar_dates' y genera metrobus.sqlite.

Uso:
  pip install pandas requests --break-system-packages
  python gtfs_to_sqlite.py
"""

import datetime
import io
import sqlite3
import time
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

# Lista oficial de operadores/concesiones de Metrobús (Área Metropolitana de València)
METROBUS_AGENCIES = {
    "València Metropolitana Nord",
    "València Metropolitana Nord-Oest",
    "València Metropolitana Oest",
    "València Metropolitana Sud",
    "La Hoya de Buñol - València",
    "Montserrat - València",
    "La Serranía - València",
    "Alto Palancia - Sagunt - València",
    "València - Benifaió",
}

REQUIRED_FILES = [
    "agency.txt",
    "routes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
]


def download_gtfs() -> zipfile.ZipFile:
    print(f"Intentando descargar GTFS desde {GTFS_URL} …")
    for intento in range(1, 4):
        try:
            resp = requests.get(GTFS_URL, timeout=60)
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
            df = pd.read_csv(f, dtype=str, keep_default_na=False)
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
    calendar = load(zf, "calendar.txt")
    calendar_dates = load(zf, "calendar_dates.txt")

    # --- 1. Filtrar agencias de Metrobús ---
    agency["agency_name_clean"] = agency["agency_name"].str.strip()
    agency_f = agency[agency["agency_name_clean"].isin(METROBUS_AGENCIES)].copy()
    agency_f.drop(columns=["agency_name_clean"], inplace=True)

    print(f"Agencias totales: {len(agency)} → Agencias de Metrobús seleccionadas: {len(agency_f)}")

    if agency_f.empty:
        raise SystemExit("Error: No se encontró ninguna agencia coincidente. Revisa los nombres de agency.txt")

    # --- 2. Filtrar rutas pertenecientes a esas agencias ---
    valid_agency_ids = set(agency_f["agency_id"]) if "agency_id" in agency_f.columns else set()
    if valid_agency_ids:
        routes_f = routes[routes["agency_id"].isin(valid_agency_ids)].copy()
    else:
        routes_f = routes.copy()

    print(f"Rutas totales: {len(routes)} → Rutas Metrobús: {len(routes_f)}")

    # --- 3. Filtrar expediciones (trips) de esas rutas ---
    valid_route_ids = set(routes_f["route_id"])
    trips_f = trips[trips["route_id"].isin(valid_route_ids)].copy()

    print(f"Trips totales: {len(trips)} → Trips Metrobús: {len(trips_f)}")

    # --- 4. Filtrar horarios (stop_times) de esos trips ---
    valid_trip_ids = set(trips_f["trip_id"])
    stop_times_f = stop_times[stop_times["trip_id"].isin(valid_trip_ids)].copy()

    # --- 5. Filtrar paradas (stops) utilizadas únicamente por esas líneas ---
    valid_stop_ids = set(stop_times_f["stop_id"])

    # Incluir también paradas padre si existen en la estructura GTFS
    if "parent_station" in stops.columns:
        parents = set(stops[stops["stop_id"].isin(valid_stop_ids)]["parent_station"])
        parents.discard("")
        valid_stop_ids.update(parents)

    stops_f = stops[stops["stop_id"].isin(valid_stop_ids)].copy()

    print(f"Paradas totales en GTFS: {len(stops)} → Paradas finales de Metrobús: {len(stops_f)}")

    # --- 6. SINTETIZAR TABLA CALENDAR DESDE CALENDAR_DATES (Corrige días de fin de semana) ---
    valid_service_ids = set(trips_f["service_id"])
    calendar_dates_f = calendar_dates[calendar_dates["service_id"].isin(valid_service_ids)].copy() if not calendar_dates.empty else calendar_dates

    if calendar.empty and not calendar_dates_f.empty:
        print("Sintetizando la tabla 'calendar' desde las fechas reales de 'calendar_dates'…")
        service_days = {}
        for _, row in calendar_dates_f.iterrows():
            if row.get("exception_type") == "1":  # Servicio activo
                sid = row["service_id"]
                dt_str = row["date"]
                try:
                    dt = datetime.datetime.strptime(dt_str, "%Y%m%d")
                    day_idx = dt.weekday()  # 0=Lunes, 1=Martes, ..., 6=Domingo
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
        print(f"Tabla 'calendar' sintetizada ({len(calendar_f)} servicios).")
    else:
        calendar_f = calendar[calendar["service_id"].isin(valid_service_ids)].copy() if not calendar.empty else calendar

    # --- 7. Volcar a SQLite ---
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

    # Crear índices para optimizar la velocidad de la API
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
    print(f"\n¡Base de datos limpia de Metrobús generada! → {OUTPUT_DB} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
