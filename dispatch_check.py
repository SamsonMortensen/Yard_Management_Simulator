import boto3
from botocore.exceptions import ClientError

#Connect to AWS
dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
table = dynamodb.Table('Yard_Inventory_Sim')

def check_appointment(container_id):
    print(f"Dispatch checking status for Container: {container_id}...")
    
    try:
        # Query the exact container record
        response = table.get_item(Key={'Container_ID': container_id})
        
        #Edge Case 1: Container is completely missing (The Dry Run)
        if 'Item' not in response:
            print("Appointment Denied.")
            print("Reason: Container not found in yard inventory. Dry run prevented.\n")
            return False
            
        item = response['Item']
        status = item.get('Current_Status')
        spot = item.get('Assigned_Spot')
        
        #Edge Case 2: In yard, but not grounded (waiting on hostler)
        if status == 'Ingate_Hold':
            print("Appointment Pending.")
            print("Reason: Unit is at the facility but still on wheels/holding. Driver must wait.\n")
            return False
            
        # Handle: Ready for pickup
        elif status == 'Parked':
            print("Appointment Approved.")
            print(f"Gate code generated. Proceed to spot {spot}.\n")
            return True
            
        #Edge Case 3: Already gone
        elif status == 'Departed':
            print("Appointment Denied.")
            print("Reason: Container has already outgated from the facility.\n")
            return False
            
    except ClientError as e:
        print(f"Database Error: {e}")
        return False

#Run the Terminal Appointment System
if __name__ == "__main__":
    print("\n--- Terminal Appointment System (TAS) Online ---\n")
    
    #Test 1: Simulate a driver asking for a container that isn't there
    check_appointment("FAKE9999999")
    
    #Test 2: Interactive check
    print("To test a real unit, look at your Streamlit Dashboard's 'Active Roster'.")
    test_id = input("Enter a Container_ID from your screen (or press Enter to quit): ")
    
    if test_id.strip():
        check_appointment(test_id.strip().upper())