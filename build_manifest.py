import urllib.request
import csv
import io
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone


def calculate_check_digit(container_id_10):
    char_map = {
        'A': 10, 'B': 12, 'C': 13, 'D': 14, 'E': 15, 'F': 16, 'G': 17, 'H': 18, 'I': 19,
        'J': 20, 'K': 21, 'L': 23, 'M': 24, 'N': 25, 'O': 26, 'P': 27, 'Q': 28, 'R': 29,
        'S': 30, 'T': 31, 'U': 32, 'V': 34, 'W': 35, 'X': 36, 'Y': 37, 'Z': 38
    }
    for i in range(10): char_map[str(i)] = i
    total = sum(char_map[c] * (2 ** i) for i, c in enumerate(container_id_10))
    return str((total % 11) % 10)

def generate_container():
    marine_prefixes = ["MSKU", "HLCU", "EMCU", "APLU", "SUDU", "ZIMU"]
    domestic_prefixes = ["JBHU", "SCHU"]
    if random.random() < 0.7:
        prefix = random.choice(marine_prefixes)
        eq = random.choices(["40_High_Cube", "20_Standard"], weights=[0.7, 0.3])[0]
        is_domestic = False
    else:
        prefix = random.choice(domestic_prefixes)
        eq = "53_Dry_Van"
        is_domestic = True
    serial = f"{random.randint(100000, 999999)}"
    cid10 = f"{prefix}{serial}"
    return f"{cid10}{calculate_check_digit(cid10)}", eq, is_domestic

def generate_manifest():
    url = 'https://data.ny.gov/api/views/v6t6-eb7h/rows.csv?accessType=DOWNLOAD'
    try:
        print("Downloading dataset from NY Open Data...")
        req = urllib.request.urlopen(url)
        content = req.read().decode('utf-8')
    except Exception as e:
        print(f"Failed to fetch data: {e}")
        return

    reader = csv.DictReader(io.StringIO(content))
    months = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5, 'June': 6, 'July': 7, 'August': 8, 'September': 9, 'October': 10, 'November': 11, 'December': 12}
    rows = list(reader)
    rows.sort(key=lambda r: (int(r.get('Year', 0)), months.get(r.get('Month', ''), 0)), reverse=True)
    latest_row = rows[0]
    volume = int(latest_row['Number of Rail Containers Moved'])
    daily_teu = volume // 30
    daily_volume = int(daily_teu / 1.65)

    print(f"Used historical data row: {latest_row['Year']} {latest_row['Month']} -> {volume} containers")
    print(f"Target daily gate moves: {daily_volume}")


    # Generate a day's worth of arrivals.
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    manifest = []
    train_volume = daily_volume // 2
    truck_volume = daily_volume - train_volume

    # Train batches (discharges at 04:00 and 18:00)
    # Group into railcars
    railcar_count = (train_volume // 2) + 1
    for r in range(railcar_count):
        hour = random.choice([4, 18])
        minute_offset = random.randint(0, 119) # 2-hour discharge
        arrival = start_of_day + timedelta(hours=hour, minutes=minute_offset)
        railcar_id = f"TTZX{random.randint(100000, 999999)}"
        
        # Bottom well
        cid_bot, eq_bot, is_dom_bot = generate_container()
        if is_dom_bot:
            # Domestic TOFC, single stack
            manifest.append({"Container_ID": cid_bot, "Equipment_Type": eq_bot, "Arrival_Time": arrival.isoformat(), "Arrival_Mode": "Rail", "Railcar_ID": railcar_id, "Well_Position": "Single", "Blocked_By": "None"})
        else:
            # Marine, double stack
            cid_top, eq_top, _ = generate_container()
            # Ensure top is marine too for simplicity
            while True:
                if not _ : break
                cid_top, eq_top, _ = generate_container()
            
            manifest.append({"Container_ID": cid_bot, "Equipment_Type": eq_bot, "Arrival_Time": arrival.isoformat(), "Arrival_Mode": "Rail", "Railcar_ID": railcar_id, "Well_Position": "Bottom", "Blocked_By": cid_top})
            manifest.append({"Container_ID": cid_top, "Equipment_Type": eq_top, "Arrival_Time": arrival.isoformat(), "Arrival_Mode": "Rail", "Railcar_ID": railcar_id, "Well_Position": "Top", "Blocked_By": "None"})

    # Bimodal truck gate (Morning surge, afternoon push)
    for _ in range(truck_volume):
        hour = int(random.normalvariate(7, 1.5)) if random.random() < 0.6 else int(random.normalvariate(14, 1.5))
        hour = max(5, min(17, hour)) # gate open 05:00-17:59
        minute = random.randint(0, 59)
        arrival = start_of_day + timedelta(hours=hour, minutes=minute)
        cid, eq, _ = generate_container()
        manifest.append({"Container_ID": cid, "Equipment_Type": eq, "Arrival_Time": arrival.isoformat(), "Arrival_Mode": "Gate", "Railcar_ID": "None", "Well_Position": "None", "Blocked_By": "None"})


    # Sort sequentially by arrival time
    manifest.sort(key=lambda x: x["Arrival_Time"])

    # truncate to daily_volume exactly since railcar grouping might slightly overshoot
    manifest = manifest[:daily_volume]

    with open(Path(__file__).parent / "historical_manifest.csv", "w", newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["Container_ID", "Equipment_Type", "Arrival_Time", "Arrival_Mode", "Railcar_ID", "Well_Position", "Blocked_By"])
        writer.writeheader()
        writer.writerows(manifest)

    print(f"Successfully generated historical_manifest.csv with {daily_volume} container records (synthesized from real PANYNJ rail volume, 2000-2015).")

if __name__ == "__main__":
    generate_manifest()
