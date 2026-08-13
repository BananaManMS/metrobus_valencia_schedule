import json
import shutil  # <-- 1. Añadir esta importación
import sqlite3
from pathlib import Path

DB_PATH = Path("./metrobus.sqlite")
OUTPUT_DIR = Path("./docs/stops")
INDEX_PATH = Path("./docs/stops_index.json")
NOJEKYLL_PATH = Path("./docs/.nojekyll")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"No se encuentra {DB_PATH} — ejecuta antes gtfs_to_sqlite.py")

    # 2. Si la carpeta de paradas existe, la borramos entera para limpiar archivos obsoletos
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Asegura la creación automática de .nojekyll
    if not NOJEKYLL_PATH.exists():
        NOJEKYLL_PATH.touch()

    # ... resto del script igual ...
