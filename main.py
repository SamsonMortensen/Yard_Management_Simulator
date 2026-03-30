import boto3
import random
import uuid
from datetime import datetime, timezone

#Initialize the DynamoDB connection
dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
table = dynamodb.Table('Yard_Inventory_Sim')

#Define our equipment types and yard spot range
equipment_types = ["53_Dry_Van", "40_High_Cube", "20_Standard", "Chassis_Bare"]
MIN_SPOT = 1000
MAX_SPOT = 5000

def generate_arrival():
    # Create a dummy container ID
    prefix = random.choice(["MSKU", "JBHT", "SCHN", "EMCU"])
    container_id = f"{prefix}{random.randint(1000000, 9999999)}"
    
    #Assign spot
    assigned_spot = random.randint(MIN_SPOT, MAX_SPOT)
    
    #Build the payload
    item = {
        'Container_ID': container_id,
        'Equipment_Type': random.choice(equipment_types),
        'Current_Status': 'Ingate_Hold',
        'Assigned_Spot': assigned_spot,
        'Arrival_Time': datetime.now(timezone.utc).isoformat(),
        'Dwell_Time_Hours': 0
    }
    return item

def push_to_cloud(num_containers):
    print(f"Generating {num_containers} new arrivals at the gate...")
    for _ in range(num_containers):
        new_container = generate_arrival()
        
        # Push to DynamoDB
        table.put_item(Item=new_container) 
        
        print(f"Arrived: {new_container['Container_ID']} | Spot: {new_container['Assigned_Spot']}")

# Simulate 5 trucks pulling up to the gate
push_to_cloud(5)