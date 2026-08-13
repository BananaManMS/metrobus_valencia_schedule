"""
Carga el 100% de los datos del GTFS de la Generalitat Valenciana en metrobus.sqlite.
Sintetiza la tabla 'calendar' desde 'calendar_dates.txt' respetando el esquema exacto.

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


def load_file(zf: zipfile.ZipFile, target_name: str) -> pd.DataFrame:
    """Busca y carga un archivo en el ZIP sin importar mayúsculas o extensiones."""
    target_clean = target_name.lower().replace(".txt", "").replace(".csv", "")
    for name in zf.namelist():
        basename = Path(name).name.lower().replace(".txt", "").replace(".csv", "")
        if basename == target_clean:
            with zf.open(name) as f:
                df = pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig")
                df.columns = df.columns.astype(str).str.strip().str.replace('\ufeff', '')
                for col in df.columns:
                    df[col] = df[col].astype(str).str.strip()
                print(f"  ✓ Cargado '{name}': {len(df)} filas")
                return df

    print(f"  ⚠️ No se encontró '{target_name}' en el zip.")
    return pd.DataFrame()


def main():
    zf = download_gtfs()

    print("\nCargando archivos del GTFS...")
    agency = load_file(zf, "agency.txt")
    routes = load_file(zf, "routes.txt")
    trips = load_file(zf, "trips.txt")
    stop_times = load_file(zf, "stop_times")
    stops = load_file(zf, "stops.txt")
    calendar_dates = load_file(zf, "calendar_dates.txt")

    if stops.empty or stop_times.empty or trips.empty:
        raise SystemExit("❌ Error: No se pudieron cargar los archivos esenciales.")

    # --- SINTETIZAR TABLA CALENDAR DESDE CALENDAR_DATES.TXT ---
    print("\nSintetizando 'calendar' a partir de 'calendar_dates.txt'...")
    service_days = {}
    if not calendar_dates.empty and "service_id" in calendar_dates.columns and "date" in calendar_dates.columns:
        for _, row in calendar_dates.iterrows():
            if str(row.get("exception_type", "1")).strip() == "1":
                sid = row["service_id"]
                dt_str = row["date"]
                try:
                    dt = datetime.datetime.strptime(dt_str, "%Y%m%d")
                    day_idx = dt.weekday()  # 0=Lunes, ..., 6=Domingo
                    if sid not in service_days:
                        service_days[sid] = [0] * 7
                    service_days[sid][day_idx] = 1
                except Exception:
                    pass

    all_service_ids = set(trips["service_id"]).union(
        set(calendar_dates["service_id"]) if not calendar_dates.empty else set()
    )

    calendar_rows = []
    for sid in all_service_ids:
        days = service_days.get(sid, [1, 1, 1, 1, 1, 1, 1])
        if sum(days) == 0:
            days = [1, 1, 1, 1, 1, 1, 1]
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

    calendar_df = pd.DataFrame(calendar_rows)

    # --- VOLCAR A SQLITE ---
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(OUTPUT_DB)

    agency.to_sql("agency", conn, index=False)
    routes.to_sql("routes", conn, index=False)
    trips.to_sql("trips", conn, index=False)
    stops.to_sql("stops", conn, index=False)
    stop_times.to_sql("stop_times", conn, index=False)
    calendar_df.to_sql("calendar", conn, index=False)
    if not calendar_dates.empty:
        calendar_dates.to_sql("calendar_dates", conn, index=False)

    # Índices para consultas instantáneas
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_stop_times_stop_id ON stop_times(stop_id);
        CREATE INDEX IF NOT EXISTS idx_stop_times_trip_id ON stop_times(trip_id);
        CREATE INDEX IF NOT EXISTS idx_trips_trip_id ON trips(trip_id);
        CREATE INDEX IF NOT EXISTS idx_trips_route_id ON trips(route_id);
        CREATE INDEX IF NOT EXISTS idx_trips_service_id ON trips(service_id);
        CREATE INDEX IF NOT EXISTS idx_routes_route_id ON routes(route_id);
        CREATE INDEX IF NOT EXISTS idx_routes_agency_id ON routes(agency_id);
        CREATE INDEX IF NOT EXISTS idx_calendar_service_id ON calendar(service_id);
        CREATE INDEX IF NOT EXISTS idx_stops_stop_id ON stops(stop_id);
    """)
    conn.commit()
    conn.close()

    size_mb = OUTPUT_DB.stat().st_size / (1024 * 1024)
    print(f"\n¡Base de datos generada con éxito! → {OUTPUT_DB} ({size_mb:.1f} MB)")
    print(f"Total paradas importadas a SQLite: {len(stops)}")


if __name__ == "__main__":
    main()
