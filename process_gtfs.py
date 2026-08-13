import zipfile
import pandas as pd
import json
import os
import shutil
from pathlib import Path

def clean_gtfs_df(filepath):
    """Lee un CSV limpiando espacios fantasma y caracteres invisibles (BOM) de la GVA"""
    df = pd.read_csv(filepath, dtype=str)
    df.columns = df.columns.str.strip().str.replace('\ufeff', '')
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    return df

def process_gva_gtfs():
    print("Iniciando procesamiento de GTFS local con IDs de agencia explícitos...")
    
    # 1. Limpiar directorio docs
    output_dir = Path("docs")
    if output_dir.exists():
        shutil.rmtree(output_dir)
        
    stops_dir = output_dir / "stops"
    stops_dir.mkdir(parents=True, exist_ok=True)

    # 2. Leer el ZIP local subido al repositorio
    zip_path = Path("20260715_020006_GenValenciana_Interurbano.zip")
    if not zip_path.exists():
        raise FileNotFoundError(f"No se encuentra el archivo ZIP en la ruta: {zip_path.absolute()}")

    print(f"Extrayendo archivo local: {zip_path.name}...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall("gtfs_temp")
        
    data_path = Path("gtfs_temp")

    # 3. Leer y desinfectar archivos TXT base
    print("Leyendo archivos TXT...")
    routes = clean_gtfs_df(data_path / 'routes.txt')
    trips = clean_gtfs_df(data_path / 'trips.txt')
    stop_times = clean_gtfs_df(data_path / 'stop_times.txt')
    stops = clean_gtfs_df(data_path / 'stops.txt')

    # --- PASO 1 y 2: Filtrado por lista explícita de agency_id ---
    print("Paso 1 y 2: Filtrando rutas mediante la lista blanca de agencias...")
    explicit_agency_ids = [
        '5904', '2358515851', '5910', '2545579951', 
        '1901190601', '1620431001', '9259', '2089140751', 
        '2211835201', '2274119701', '2509810701', '5202'
    ]

    valid_routes = routes[routes['agency_id'].isin(explicit_agency_ids)]
    valid_route_ids = valid_routes['route_id'].unique()
    print(f"Rutas válidas encontradas: {len(valid_route_ids)}")

    # --- PASO 3: Buscar en trips.txt los service_id y trip_id ---
    print("Paso 3: Extrayendo viajes (trips) asociados a las rutas...")
    valid_trips = trips[trips['route_id'].isin(valid_route_ids)]
    
    # --- PASO 4: Traducir días de operación usando calendar_dates.txt ---
    #             (este GTFS de la GVA no trae calendar.txt — solo excepciones
    #             puntuales por fecha, así que el día de la semana se deriva
    #             de cada fecha concreta y se generaliza a partir de ahí)
    print("Paso 4: Mapeando service_id a días de la semana...")
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    service_days = {}

    if (data_path / 'calendar_dates.txt').exists():
        calendar_dates = clean_gtfs_df(data_path / 'calendar_dates.txt')
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

        # exception_type == '2' = servicio EXCLUIDO ese día concreto.
        # Si calendar.txt ya marcaba ese día como activo para el service_id,
        # aquí lo quitamos para no dejarlo como "opera" incorrectamente.
        excluded_calendar = calendar_dates[
            (calendar_dates['service_id'].isin(valid_services)) &
            (calendar_dates['exception_type'] == '2')
        ]
        for _, row in excluded_calendar.iterrows():
            s_id = row['service_id']
            date_str = row['date']
            try:
                date_obj = pd.to_datetime(date_str, format='%Y%m%d')
                day_name = day_names[date_obj.weekday()]
                if s_id in service_days:
                    service_days[s_id].discard(day_name)
            except Exception:
                pass

    # --- PASO 5: Consultar stop_times.txt con el trip_id ---
    print("Paso 5: Consultando stop_times para obtener paradas, horas de salida y secuencias...")
    valid_trip_ids = valid_trips['trip_id'].unique()
    valid_stop_times = stop_times[stop_times['trip_id'].isin(valid_trip_ids)].copy()
    
    merged_data = valid_stop_times.merge(valid_trips[['trip_id', 'route_id', 'service_id']], on='trip_id')
    merged_data = merged_data.merge(valid_routes[['route_id', 'route_short_name']], on='route_id')

    # --- PASO 6: Asociar con stops.txt para obtener los datos finales de la parada ---
    print("Paso 6: Asociando con stops.txt y generando los JSON finales...")
    valid_stop_ids = valid_stop_times['stop_id'].unique()
    valid_stops = stops[stops['stop_id'].isin(valid_stop_ids)]

    stops_global_list = []
    days_order = {"Lunes": 1, "Martes": 2, "Miércoles": 3, "Jueves": 4, "Viernes": 5, "Sábado": 6, "Domingo": 7, "Días no especificados": 8}

    for _, stop_row in valid_stops.iterrows():
        stop_id = stop_row['stop_id']
        stop_name = stop_row.get('stop_name', 'Desconocido')
        
        try:
            stop_lat = float(stop_row['stop_lat'])
            stop_lon = float(stop_row['stop_lon'])
        except:
            continue
            
        stop_records = merged_data[merged_data['stop_id'] == stop_id]
        lines_dict = {}
        departures_list = []

        for _, record in stop_records.iterrows():
            line_name = record['route_short_name']
            s_id = record['service_id']
            operating_days = service_days.get(s_id, set())
            
            if not operating_days:
                operating_days = set(["Días no especificados"])
                
            if line_name not in lines_dict:
                lines_dict[line_name] = set()
            lines_dict[line_name].update(operating_days)

            # Hora de salida real de este viaje por esta parada (lo que
            # faltaba: sin esto el JSON no tenía ninguna hora de salida).
            departures_list.append({
                "line": line_name,
                "trip_id": record['trip_id'],
                "departure_time": record['departure_time'],
                "stop_sequence": record['stop_sequence'],
                "service_id": s_id,
                "days": sorted(operating_days, key=lambda x: days_order.get(x, 99)),
            })

        # Orden cronológico por hora de salida — importante para que la
        # app pueda pintar directamente "próximas salidas" sin reordenar.
        departures_list.sort(key=lambda d: d["departure_time"])

        formatted_lines = []
        for line, days_set in lines_dict.items():
            sorted_days = sorted(list(days_set), key=lambda x: days_order.get(x, 99))
            formatted_lines.append({
                "line": line,
                "days": sorted_days
            })
            
        formatted_lines = sorted(formatted_lines, key=lambda x: x['line'])
        
        # Guardar en el índice global
        stops_global_list.append({
            "id": stop_id,
            "name": stop_name,
            "lat": stop_lat,
            "lon": stop_lon,
            "lines": [line_data['line'] for line_data in formatted_lines]
        })
        
        # Guardar el JSON individual de la parada
        stop_detail_json = {
            "id": stop_id,
            "name": stop_name,
            "lat": stop_lat,
            "lon": stop_lon,
            "lines": formatted_lines,
            "departures": departures_list,
        }
        
        file_path = stops_dir / f"{stop_id}.json"
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(stop_detail_json, f, ensure_ascii=False, separators=(',', ':'))

    # Guardar el JSON global index
    print("Generando listado global (stops_index.json)...")
    with open(output_dir / 'stops_index.json', 'w', encoding='utf-8') as f:
        json.dump(stops_global_list, f, ensure_ascii=False, separators=(',', ':'))

    # --- PASO 7: Generar catálogo de líneas (lines.json) ---
    # Diccionario route_short_name -> route_long_name, para no repetir el
    # nombre largo en cada parada/salida. Se genera aparte a partir de
    # valid_routes, sin tocar cómo se construyen stops_index.json ni los
    # JSON individuales de cada parada.
    print("Generando catálogo de líneas (lines.json)...")
    lines_catalog = {}
    for _, route_row in valid_routes.iterrows():
        short_name = route_row.get('route_short_name', '')
        long_name = route_row.get('route_long_name', '')
        if short_name and short_name not in lines_catalog:
            lines_catalog[short_name] = long_name

    with open(output_dir / 'lines.json', 'w', encoding='utf-8') as f:
        json.dump(lines_catalog, f, ensure_ascii=False, separators=(',', ':'))

    print("Limpiando archivos temporales...")
    shutil.rmtree("gtfs_temp", ignore_errors=True)
        
    print("¡Proceso completado con éxito usando los IDs de agencia definidos!")

if __name__ == "__main__":
    process_gva_gtfs()
