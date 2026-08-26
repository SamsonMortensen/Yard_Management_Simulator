import urllib.request
import csv
import io
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone

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
    daily_volume = volume // 30

    print(f"Used historical data row: {latest_row['Year']} {latest_row['Month']} -> {volume} containers")
    print(f"Target daily gate moves: {daily_volume}")

    equipment_types = ["53_Dry_Van", "40_High_Cube", "20_Standard", "Chassis_Bare"]
    prefixes = ["MSKU", "JBHT", "SCHN", "EMCU", "APLU", "HLCU", "SUDU", "ZIMU"]

    # Generate a day's worth of arrivals.
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    manifest = []
    for _ in range(daily_volume):
        # Skew arrival time toward business hours (normal distribution around 12:00 PM)
        hour = int(random.normalvariate(12, 4))
        hour = max(0, min(23, hour))
        minute = random.randint(0, 59)
        arrival = start_of_day + timedelta(hours=hour, minutes=minute)
        
        prefix = random.choice(prefixes)
        cid = f"{prefix}{random.randint(1000000, 9999999)}"
        eq = random.choice(equipment_types)
        manifest.append({
            "Container_ID": cid,
            "Equipment_Type": eq,
            "Arrival_Time": arrival.isoformat()
        })

    # Sort sequentially by arrival time
    manifest.sort(key=lambda x: x["Arrival_Time"])

    with open(Path(__file__).parent / "historical_manifest.csv", "w", newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["Container_ID", "Equipment_Type", "Arrival_Time"])
        writer.writeheader()
        writer.writerows(manifest)

    
    print(f"Successfully generated historical_manifest.csv with {daily_volume} container records (synthesized from real PANYNJ rail volume, 2000-2015).")

if __name__ == "__main__":
    generate_manifest()
