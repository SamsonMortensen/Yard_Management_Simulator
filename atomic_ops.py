"""Atomic state transition plus parking-spot release."""
from boto3.dynamodb.types import TypeSerializer
from boto3.dynamodb.conditions import Attr


def transition_and_release(table, container_id, spot, update_expression, values,
                           expected_status, reservation_key=None,
                           expected_reservation=None):
    """Update a container and release its ground reservation as one transaction."""
    reservation_key = reservation_key or f"SPOT#{spot}"
    condition = Attr('Current_Status').eq(expected_status)
    if expected_reservation:
        condition = condition & Attr('Ground_Reservation_ID').eq(expected_reservation)
    if hasattr(table, "atomic_update_and_delete"):
        return table.atomic_update_and_delete(
            Key={"Container_ID": container_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=values,
            ConditionExpression=condition,
            DeleteKey={"Container_ID": reservation_key},
        )

    serializer = TypeSerializer()
    serialize_map = lambda mapping: {k: serializer.serialize(v) for k, v in mapping.items()}
    table.meta.client.transact_write_items(TransactItems=[
        {"Update": {
            "TableName": table.name,
            "Key": serialize_map({"Container_ID": container_id}),
            "UpdateExpression": update_expression,
            "ConditionExpression": (
                "#status = :expected"
                + (" AND #reservation = :expected_reservation" if expected_reservation else "")
            ),
            "ExpressionAttributeNames": {
                "#status": "Current_Status",
                **({"#reservation": "Ground_Reservation_ID"} if expected_reservation else {}),
            },
            "ExpressionAttributeValues": serialize_map({
                **values,
                ":expected": expected_status,
                **({":expected_reservation": expected_reservation}
                   if expected_reservation else {}),
            }),
        }},
        {"Delete": {
            "TableName": table.name,
            "Key": serialize_map({"Container_ID": reservation_key}),
        }},
    ])
    return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def relocate_ground_unit(table, container, location, employee):
    """Move one blocking container to a new ground tier atomically."""
    old_reservation = container.get(
        "Ground_Reservation_ID", f"SPOT#{container['Assigned_Spot']}"
    )
    new_reservation = {
        "Container_ID": location["Ground_Reservation_ID"],
        "Type": "Ground_Reservation",
        "Assigned_Spot": location["Assigned_Spot"],
        "Ground_Tier": location["Ground_Tier"],
        "Yard_Block": location["Yard_Block"],
    }
    values = {
        ':spot': location['Assigned_Spot'],
        ':tier': location['Ground_Tier'],
        ':block': location['Yard_Block'],
        ':reservation': location['Ground_Reservation_ID'],
        ':old_reservation': old_reservation,
        ':employee': employee,
    }
    condition = (
        Attr('Current_Status').eq('Parked')
        & Attr('Ground_Reservation_ID').eq(old_reservation)
    )
    if hasattr(table, "atomic_relocate"):
        return table.atomic_relocate(
            Key={"Container_ID": container["Container_ID"]},
            UpdateExpression=(
                "set Assigned_Spot = :spot, Ground_Tier = :tier, Yard_Block = :block, "
                "Ground_Reservation_ID = :reservation, Last_Rehandled_By = :employee"
            ),
            ExpressionAttributeValues=values,
            ConditionExpression=condition,
            OldReservationKey={"Container_ID": old_reservation},
            NewReservationItem=new_reservation,
        )

    serializer = TypeSerializer()
    serialize_map = lambda mapping: {k: serializer.serialize(v) for k, v in mapping.items()}
    table.meta.client.transact_write_items(TransactItems=[
        {"Put": {
            "TableName": table.name,
            "Item": serialize_map(new_reservation),
            "ConditionExpression": "attribute_not_exists(Container_ID)",
        }},
        {"Update": {
            "TableName": table.name,
            "Key": serialize_map({"Container_ID": container["Container_ID"]}),
            "UpdateExpression": (
                "set Assigned_Spot = :spot, Ground_Tier = :tier, Yard_Block = :block, "
                "Ground_Reservation_ID = :reservation, Last_Rehandled_By = :employee"
            ),
            "ConditionExpression": (
                "#status = :parked AND #reservation = :old_reservation"
            ),
            "ExpressionAttributeNames": {
                "#status": "Current_Status",
                "#reservation": "Ground_Reservation_ID",
            },
            "ExpressionAttributeValues": serialize_map({**values, ':parked': 'Parked'}),
        }},
        {"Delete": {
            "TableName": table.name,
            "Key": serialize_map({"Container_ID": old_reservation}),
        }},
    ])
