import boto3
import time
from boto3.dynamodb.conditions import Attr
import random

#Connect to AWS
dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
table = dynamodb.Table('Yard_Inventory_Sim')

#Shift Roster (employee IDs)
active_hostlers = ["EMP-104", "EMP-227", "EMP-309", "EMP-412"]

def move_container():
    # Scan the database for units at the gate
    response = table.scan(
        FilterExpression=Attr('Current_Status').eq('Ingate_Hold')
    )
    gate_items = response.get('Items', [])

    if not gate_items:
        print("Yard is clear. No containers waiting at the gate.")
        return False

    #Grab the first container in line
    container = gate_items[0]
    container_id = container['Container_ID']
    assigned_spot = container['Assigned_Spot']

    #Assign a driver
    driver = random.choice(active_hostlers)

    print(f"Hostler dispatching to Gate... Grabbing {container_id}")
    time.sleep(2) # Simulating the physical drive time

    #Update the container's status in the cloud
    table.update_item(
        Key={'Container_ID': container_id},
        UpdateExpression="set Current_Status = :s, Parked_By_Employee =:e",
        ExpressionAttributeValues={':s': 'Parked', ':e': driver}
    )

    print(f"Dropped {container_id} at parking spot {assigned_spot}\n")
    return True

# Run the Hostler Shift
print("Starting Hostler Shift...")
while True:
    moved = move_container()
    if not moved:
        break # Clock out if the gate is empty
    time.sleep(3) # Short break between moves