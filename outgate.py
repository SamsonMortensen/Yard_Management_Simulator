import boto3
import random
import time
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Attr
from decimal import Decimal


#Connect to AWS
dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
table = dynamodb.Table('Yard_Inventory_Sim')

def process_outgate():
    #Scan the database ONLY for parked containers
    response = table.scan(
        FilterExpression=Attr('Current_Status').eq('Parked')
    )
    parked_items = response.get('Items', [])

    if not parked_items:
        print("No parked containers ready for outgate.")
        return False

    # Randomly select a driver picking up a container
    container = random.choice(parked_items)
    container_id = container['Container_ID']
    arrival_time_str = container['Arrival_Time']

    print(f"Outgate driver arrived... Searching for {container_id}")
    time.sleep(2) # Simulating gate check and hooking up the chassis

    # Calculate the Dwell Time
    #Convert the ISO string
    arrival_time = datetime.fromisoformat(arrival_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    
    # Calculate the difference in hours
    raw_hours = round((now - arrival_time).total_seconds() / 3600, 4)
    dwell_hours = Decimal(str(raw_hours))

    
    #update the record in AWS
    table.update_item(
        Key={'Container_ID': container_id},
        UpdateExpression="set Current_Status = :s, Dwell_Time_Hours = :d",
        ExpressionAttributeValues={
            ':s': 'Departed',
            ':d': dwell_hours
        }
    )

    print(f"{container_id} has left the yard.")
    print(f"Final Dwell Time logged: {dwell_hours} hours.\n")
    return True

# Run the Outgate Shift
print("Starting Outgate Shift...")
while True:
    moved = process_outgate()
    if not moved:
        break # Clock out if the yard is empty
    time.sleep(4) #break before the next truck arrives