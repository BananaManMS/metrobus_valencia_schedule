import urllib.request
import zipfile
import io
import pandas as pd
import json
import os
import shutil
from pathlib import Path

def process_gva_gtfs():
    print("Iniciando procesamiento de GTFS...")
    
    # 1. Limpiar directorio docs para evitar amontonar archivos huérfanos
    output_dir = Path("docs")
    if output_dir.exists():
        print("Limpiando datos antiguos de docs/...")
        shutil.rmtree(output_dir)
        
    stops_dir = output_dir / "stops"
    stops_dir.mkdir(parents=True, exist_ok=True)

    # 2. Descargar y extraer GTFS
    print("Descargando GTFS desde Dades Obertes GVA...")
    url = "https://dadesobertes.gva.es/dataset/2f380ffd-b389-4ff4-9f7c-be92b30fbf28/resource/3c8a2e6b-5b5e-49f5-872f-5f33fcd52547/download/gtfs.zip"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    response = urllib.request.urlopen(req)
    
    print("Extrayendo archivos...")
    with zipfile.ZipFile(io.BytesIO(response.read())) as zip_ref:
        zip_ref.extractall("gtfs_temp")
        
    data_path = Path("gtfs_temp")

    # 3. Leer archivos básicos
    print("Leyendo archivos TXT...")
    agency = pd.read_csv(data_path / 'agency.txt', dtype=str)
    routes = pd.read_csv(data_path / 'routes.txt', dtype=str)
    trips = pd.read_csv(data_path / 'trips.txt', dtype=str)
    stop_times = pd.read_csv(data_path / 'stop_times.txt', dtype=str)
    stops = pd.read_csv(data_path / 'stops.txt', dtype=str)

    # --- FASE 2: Filtrado robusto ATMV (Metrobús) ---
    print("Filtrando datos para ATMV (Metrobús)...")
    # Buscamos que CONTENGA la palabra metgovalencia, ignorando mayúsculas y espacios erróneos
    agency['agency_url'] = agency['agency_url'].fillna('')
    valid_agencies = agency[agency['agency_url'].str.contains('metgovalencia', case=False)]['agency_id'].tolist()
    print(f"IDs de agencias de Metrobús encontradas: {valid_agencies}")

    valid_routes = routes[routes['agency_id'].isin(valid_agencies)]
    valid_trips = trips[trips['route_id'].isin(valid_routes['route_id'])]
    valid_stop_times = stop_times[stop_times['trip_id'].isin(valid_trips['trip_id'])]
    valid_stop_ids = valid_stop_times['stop_id'].unique().tolist()
    valid_stops = stops[stops['stop_id'].isin(valid_stop_ids)]
    print(f"Total paradas a procesar: {len(valid_stops)}")

    # --- FASE 3: Traducción de fechas ---
    print("Buscando días de operación...")
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    service_days = {}
    
    # Intentar leer calendar.txt si la GVA lo ha incluido
    if (data_path / 'calendar.txt').exists():
        calendar = pd.read_csv(data_path / 'calendar.txt', dtype=str)
        day_cols = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        for _, row in calendar.iterrows():
            s_id = row['service_id']
            active_days = set()
            for idx, col in enumerate(day_cols):
                if str(row.get(col, '0')) == '1':
                    active_days.add(day_names[idx])
            if active_days:
                service_days[s_id] = active_days

    # Añadir excepciones y fechas de calendar_dates.txt
    if (data_path / 'calendar_dates.txt').exists():
        calendar_dates = pd.read_csv(data_path / 'calendar_dates.txt', dtype=str)
        valid_services = valid_trips['service_id'].unique().tolist()
        valid_calendar = calendar_dates[
            (calendar_dates['service_id'].isin(valid_services)) & 
            (calendar_dates['exception_type'] == '1')
        ]
        
        for _, row in valid_calendar.iterrows():
            s_id = row['service_id']
            date_str = row['date']
            try:
                date_obj = pd.to_datetime(date_str, format='%Y%m%d')
                day_name = day_names[date_obj.weekday()]
                if s_id not in service_days:
                    service_days[s_id] = set()
                service_days[s_id].add(day_name)
            except Exception:
                pass
            
    # --- FASE 4: Cruce final y agrupación ---
    print("Cruzando información de líneas y paradas...")
    merged_data = valid_stop_times.merge(valid_trips[['trip_id', 'route_id', 'service_id']], on='trip_id')
    merged_data = merged_data.merge(valid_routes[['route_id', 'route_short_name']], on='route_id')

    stops_global_list = []
    days_order = {"Lunes": 1, "Martes": 2, "Miércoles": 3, "Jueves": 4, "Viernes": 5, "Sábado": 6, "Domingo": 7, "Días no especificados": 8}

    for _, stop_row in valid_stops.iterrows():
        stop_id = stop_row['stop_id']
        stop_name = stop_row.get('stop_name', 'Desconocido')
        stop_lat = float(stop_row['stop_lat'])
        stop_lon = float(stop_row['stop_lon'])
        
        stop_records = merged_data[merged_data['stop_id'] == stop_id]
        lines_dict = {}
        
        for _, record in stop_records.iterrows():
            line_name = record['route_short_name']
            s_id = record['service_id']
            operating_days = service_days.get(s_id, set())
            
            # ¡CRÍTICO! Si el GTFS no tiene días guardados para este servicio, 
            # NO ignoramos la línea. La guardamos indicando que no hay días especificados.
            if not operating_days:
                operating_days = set(["Días no especificados"])
                
            if line_name not in lines_dict:
                lines_dict[line_name] = set()
            lines_dict[line_name].update(operating_days)
            
        formatted_lines = []
        for line, days_set in lines_dict.items():
            sorted_days = sorted(list(days_set), key=lambda x: days_order.get(x, 99))
            formatted_lines.append({
                "line": line,
                "days": sorted_days
            })
            
        formatted_lines = sorted(formatted_lines, key=lambda x: x['line'])
        
        # --- NUEVO: Construir el JSON Global con Nombre y Líneas ---
        stops_global_list.append({
            "id": stop_id,
            "name": stop_name,
            "lat": stop_lat,
            "lon": stop_lon,
            "lines": [line_data['line'] for line_data in formatted_lines] # Solo saca los nombres (Ej: ["L150", "L165"])
        })
        
        # --- FASE 5: Generar el JSON individual ---
        stop_detail_json = {
            "id": stop_id,
            "name": stop_name,
            "lat": stop_lat,
            "lon": stop_lon,
            "lines": formatted_lines
        }
        
        file_path = stops_dir / f"{stop_id}.json"
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(stop_detail_json, f, ensure_ascii=False, separators=(',', ':'))

    # Generar el JSON global index
    print("Generando listado global...")
    with open(output_dir / 'stops_index.json', 'w', encoding='utf-8') as f:
        json.dump(stops_global_list, f, ensure_ascii=False, separators=(',', ':'))
        
    print("Limpiando archivos temporales...")
    shutil.rmtree("gtfs_temp", ignore_errors=True)
        
    print("Proceso completado con éxito. Archivos guardados en 'docs/'")

if __name__ == "__main__":
    process_gva_gtfs()
