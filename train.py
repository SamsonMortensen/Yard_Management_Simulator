"""Outbound train model and schedule manager for intermodal export operations.

Represents an outbound intermodal train consist with:
- Finite wellcar capacity (e.g. N double-stack wellcars = 2N container slots)
- Scheduled cutoff time for loading
- Well loading status (Bottom and Top positions)
- Train departure event: atomically departs all loaded containers (Current_Status='Departed', Departure_Mode='Rail')
"""
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


class OutboundTrain:
    def __init__(self, train_id="TR-OUT-01", well_capacity=5, cutoff_minutes=60.0):
        self.train_id = train_id
        self.well_capacity = well_capacity
        self.cutoff_minutes = cutoff_minutes
        self.created_at = datetime.now(timezone.utc)
        
        # Structure: { 'TTZX00001': {'Bottom': cid or None, 'Top': cid or None}, ... }
        self.wells = {}
        for i in range(1, well_capacity + 1):
            car_id = f"TTZX{i:05d}"
            self.wells[car_id] = {'Bottom': None, 'Top': None}
        
        self.departed = False

    def can_load(self, car_id, position, container_id=None):
        """Validates well existence and enforces bottom-before-top loading precedence."""
        if car_id not in self.wells:
            return False, f"Railcar {car_id} not in consist"
        
        slot = self.wells[car_id]
        if position in ['Bottom', 'Single', 'None']:
            if slot['Bottom'] is not None and (container_id is None or slot['Bottom'] != container_id):
                return False, f"Bottom slot of {car_id} already occupied by {slot['Bottom']}"
            return True, "OK"
        elif position == 'Top':
            if slot['Bottom'] is None:
                return False, f"Cannot load Top slot of {car_id} before Bottom slot is loaded"
            if slot['Top'] is not None and (container_id is None or slot['Top'] != container_id):
                return False, f"Top slot of {car_id} already occupied by {slot['Top']}"
            return True, "OK"
        return False, f"Invalid position {position}"

    def load_container(self, car_id, position, container_id):
        """Records a container loaded into a specific well slot."""
        ok, reason = self.can_load(car_id, position, container_id)
        if not ok:
            raise ValueError(f"Cannot load container {container_id} into {car_id}/{position}: {reason}")
        
        pos_key = 'Bottom' if position in ['Bottom', 'Single', 'None'] else 'Top'
        self.wells[car_id][pos_key] = container_id
        return True

    def get_loaded_containers(self):
        """Returns a list of all container IDs loaded onto the train."""
        loaded = []
        for car_id, slots in self.wells.items():
            if slots['Bottom']:
                loaded.append(slots['Bottom'])
            if slots['Top']:
                loaded.append(slots['Top'])
        return loaded

    def depart(self, table):
        """Executes the train departure event, atomically marking all loaded units as Departed via Rail."""
        loaded = self.get_loaded_containers()
        departed_count = 0
        
        for cid in loaded:
            try:
                table.update_item(
                    Key={'Container_ID': cid},
                    UpdateExpression="set Current_Status = :s, Departure_Mode = :m, Outbound_Train_ID = :t",
                    ExpressionAttributeValues={
                        ':s': 'Departed',
                        ':m': 'Rail',
                        ':t': self.train_id
                    },
                    ConditionExpression=Attr('Current_Status').eq('Loaded_Rail')
                )
                departed_count += 1
            except ClientError as e:
                if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
        
        self.departed = True
        return departed_count


# Global singleton / registry for simulator use
_ACTIVE_TRAINS = {}

def get_outbound_train(train_id="TR-OUT-01", well_capacity=5, cutoff_minutes=60.0):
    global _ACTIVE_TRAINS
    if train_id not in _ACTIVE_TRAINS:
        _ACTIVE_TRAINS[train_id] = OutboundTrain(train_id, well_capacity, cutoff_minutes)
    return _ACTIVE_TRAINS[train_id]

def reset_trains():
    global _ACTIVE_TRAINS
    _ACTIVE_TRAINS = {}
