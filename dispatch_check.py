"""Terminal Appointment System. The authorization check at the in-gate.

An outside driver cannot just pull in and take a box. They get checked
against the yard first, and this is that check. It answers one question:
is this unit actually on the ground and available to this driver right now.

Denied for the cases that would waste the trip:

  not in the yard        never arrived, or already gone. This is the dry run
                         the whole thing exists to prevent.
  still on the train     Trackside_Hold, Buffer_Hold, Rendezvous_Wait or
                         Claimed. It is here but nobody can hand it over yet.
  Direction is Export    it is staged for a train, not for a customer.
  already departed       somebody already took it.
  anything unrecognized  deny and send it to the tower rather than guess.

Approved only for a Parked Import. On the ground, and waiting for pickup.
"""
from botocore.exceptions import ClientError

from config import get_table

table = get_table()


def check_appointment(container_id):
    print(f"Dispatch checking status for Container: {container_id}...")

    try:
        # Query the exact container record
        response = table.get_item(Key={'Container_ID': container_id})

        # Edge Case 1: Container is completely missing (The Dry Run)
        if 'Item' not in response:
            print("Appointment Denied.")
            print("Reason: Container not found in yard inventory. Dry run prevented.\n")
            return False

        item = response['Item']
        status = item.get('Current_Status')
        spot = item.get('Assigned_Spot')
        direction = item.get('Direction', 'Import')

        # Edge Case 2: Outbound export unit staged for train, not customer pickup
        if direction == 'Export':
            print("Appointment Denied.")
            print("Reason: Unit is an outbound export container staged for rail, not customer road pickup.\n")
            return False

        # Edge Case 3: In yard, but not grounded / still holding
        if status in ('Ingate_Hold', 'Claimed', 'Buffer_Hold', 'Rendezvous_Wait', 'Trackside_Hold', 'Awaiting_Rail'):
            print("Appointment Pending.")
            print("Reason: Unit is at the facility but still on wheels/holding. Driver must wait.\n")
            return False

        # Handle: Ready for customer pickup
        elif status == 'Parked':
            print("Appointment Approved.")
            print(f"Gate code generated. Proceed to spot {spot}.\n")
            return True

        # Edge Case 4: Already departed
        elif status == 'Departed':
            print("Appointment Denied.")
            print("Reason: Container has already outgated from the facility.\n")
            return False

        # Edge Case 5: Unrecognized status: deny rather than guess
        print("Appointment Denied.")
        print(f"Reason: Unit is in an unrecognized status ({status}). Escalate to the tower.\n")
        return False

    except ClientError as e:
        print(f"Database Error: {e}")
        return False


# Run the Terminal Appointment System
if __name__ == "__main__":
    print("\n--- Terminal Appointment System (TAS) Online ---\n")

    # Test 1: Simulate a driver asking for a container that isn't there
    check_appointment("FAKE9999999")

    # Test 2: Interactive check
    print("To test a real unit, look at your Streamlit Dashboard's 'Active Roster'.")
    test_id = input("Enter a Container_ID from your screen (or press Enter to quit): ")

    if test_id.strip():
        check_appointment(test_id.strip().upper())
