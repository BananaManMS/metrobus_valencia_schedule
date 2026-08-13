"""
Carga el 100% del GTFS de la Generalitat en metrobus.sqlite.
Limpia encabezados y valores directamente en Python para que los
índices de SQLite funcionen a máxima velocidad.

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


def download_gtfs() -> zipfile.ZipFile:
    print(f"Descargando GTFS desde {GTFS_URL} …")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
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


def load_flexible(zf: zipfile.ZipFile, target_base_name: str) -> pd.DataFrame:
    target = target_base_name.lower().replace(".txt", "").replace(".csv", "")
    
    matched_file = None
    for name in zf.namelist():
        basename = Path(name).name.lower().replace(".txt", "").replace(".csv", "")
        if basename == target:
            matched_file = name
            break

    if matched_file:
        with zf.open(matched_file) as f:
            df = pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            # Eliminar caracteres BOM \ufeff y espacios en nombres de columnas
            df.columns = df.columns.astype(str).str.strip().str.replace('\ufeff', '')
            # Limpiar espacios en blanco en todos los valores
            for col in df.columns:
                df[col] = df[col].astype(str).str.strip()
            print(f"  ✓ Cargado '{matched_file}': {len(df)} filas")
            return df

    print(f"  ⚠️ No se encontró '{target_base_name}' en el zip.")
    return pd.DataFrame()


def main():
    zf = download_gtfs()

    print("\nCargando el 100% de los archivos del GTFS...")
    agency = load_flexible(zf, "agency.txt")
    routes = load_flexible(zf, "routes.txt")
    trips = load_flexible(zf, "trips.txt")
    stop_times = load_flexible(zf, "stop_times")
    stops = load_flexible(zf, "stops.txt")
    calendar = load_flexible(zf, "calendar.txt")
    calendar_dates = load_flexible(zf, "calendar_dates.txt")

    if stops.empty or stop_times.empty or trips.empty:
        raise SystemExit("❌ Error: No se pudieron cargar los archivos esenciales (stops, stop_times, trips).")

    # --- SINTETIZAR TABLA CALENDAR DESDE CALENDAR_DATES ---
    all_service_ids = set(trips["service_id"]).union(
        set(calendar_dates["service_id"]) if not calendar_dates.empty else set()
    )
    existing_calendar_ids = set(calendar["service_id"]) if not calendar.empty else set()

    service_days = {}
    if not calendar_dates.empty and "service_id" in calendar_dates.columns and "date" in calendar_dates.columns:
        for _, row in calendar_dates.iterrows():
            if str(row.get("exception_type", "1")).strip() == "1":
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
    if not calendar.empty:
        for _, row in calendar.iterrows():
            calendar_rows.append({
                "service_id": str(row["service_id"]).strip(),
                "monday": int(row.get("monday", 1)),
                "tuesday": int(row.get("tuesday", 1)),
                "wednesday": int(row.get("wednesday", 1)),
                "thursday": int(row.get("thursday", 1)),
                "friday": int(row.get("friday", 1)),
                "saturday": int(row.get("saturday", 1)),
                "sunday": int(row.get("sunday", 1)),
            })

    for sid in all_service_ids:
        if sid not in existing_calendar_ids:
            days = service_days.get(sid, [1, 1, 1, 1, 1, 1, 1])
            calendar_rows.append({
                "service_id": str(sid).strip(),
                "monday": days[0],
                "tuesday": days[1],
                "wednesday": days[2],
                "thursday": days[3],
                "friday": days[4],
                "saturday": days[5],
                "sunday": days[6],
            })

    calendar_f = pd.DataFrame(calendar_rows)

    # --- VOLCAR A SQLITE ---
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(OUTPUT_DB)

    if not agency.empty:
        agency.to_sql("agency", conn, index=False)
    if not routes.empty:
        routes.to_sql("routes", conn, index=False)
    if not trips.empty:
        trips.to_sql("trips", conn, index=False)
    if not stops.empty:
        stops.to_sql("stops", conn, index=False)
    if not stop_times.empty:
        stop_times.to_sql("stop_times", conn, index=False)
    if not calendar_f.empty:
        calendar_f.to_sql("calendar", conn, index=False)
    if not calendar_dates.empty:
        calendar_dates.to_sql("calendar_dates", conn, index=False)

    # Crear índices limpios para búsquedas ultrarrápidas
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_stop_times_stop_id ON stop_times(stop_id);",
        "CREATE INDEX IF NOT EXISTS idx_stop_times_trip_id ON stop_times(trip_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_trip_id ON trips(trip_id);",
        "CREATE INDEX IF NOT EXISTS idx_trips_route_id ON trips(route_id);",
        "CREATE INDEX IF NOT EXISTS idx_routes_route_id ON routes(route_id);",
    ]

    if not routes.empty and "agency_id" in routes.columns:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_routes_agency_id ON routes(agency_id);")
    if not stops.empty and "parent_station" in stops.columns:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_stops_parent ON stops(parent_station);")
    if not calendar_f.empty:
        indexes.append("CREATE INDEX IF NOT EXISTS idx_calendar_service_id ON calendar(service_id);")

    conn.executescript("\n".join(indexes))
    conn.commit()
    conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 * 1024)
    print(f"\n¡Base de datos generada e indexada! → {OUTPUT_DB} ({size_mb:.1f} MB)")
    print(f"Total paradas importadas a SQLite: {len(stops)}")


if __name__ == "__main__":
    main()
