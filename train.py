"""Plan and depart a constrained outbound intermodal consist.

Each well has a length, gross stack-weight limit, and destination block. A
40- or 53-foot unit occupies one long bottom or top position. Two 20-foot
units may share the bottom as A/B positions; a long top is allowed only after
both bottom twenties are present. Top load weight is limited relative to its
foundation, and every unit in a well must share a destination block.

The crane asks for a valid plan immediately before loading. The train reserves
that physical slot under a lock, the database commits ``Loaded_Rail``, and a
failed database write releases the reservation. At cutoff the loaded consist
departs in one transaction (up to DynamoDB's 100-action transaction limit).
"""
from datetime import datetime, timedelta, timezone
import threading

from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer


MAX_CONTAINER_WEIGHT_LBS = 67_200
MAX_WELL_STACK_WEIGHT_LBS = 134_400
MAX_TOP_OVER_BOTTOM_LBS = 10_000


def equipment_length(equipment_type):
    if str(equipment_type).startswith("20"):
        return 20
    if str(equipment_type).startswith("53"):
        return 53
    return 40


class OutboundTrain:
    def __init__(self, train_id="TR-OUT-01", well_capacity=5, cutoff_minutes=60.0,
                 well_lengths=None):
        self.train_id = train_id
        self.well_capacity = int(well_capacity)
        self.cutoff_minutes = float(cutoff_minutes)
        self.created_at = datetime.now(timezone.utc)
        self.cutoff_time = self.created_at + timedelta(minutes=self.cutoff_minutes)
        self._lock = threading.RLock()
        lengths = list(well_lengths or ())

        self.wells = {}
        for index in range(1, self.well_capacity + 1):
            length = lengths[index - 1] if index <= len(lengths) else (53 if index % 2 else 40)
            self.wells[f"TTZX{index:05d}"] = {
                "Well_Length_Ft": int(length),
                "Max_Stack_Weight_Lbs": MAX_WELL_STACK_WEIGHT_LBS,
                "Destination_Block": None,
                "Bottom": None,
                "Bottom_A": None,
                "Bottom_B": None,
                "Top": None,
                "Weights": {},
                "Equipment": {},
            }
        self.departed = False

    def _item(self, container_id, equipment_type=None, gross_weight_lbs=None,
              destination_block=None):
        return {
            "Container_ID": container_id,
            "Equipment_Type": equipment_type or "40_High_Cube",
            "Gross_Weight_Lbs": int(gross_weight_lbs or 40_000),
            "Destination_Block": destination_block or "UNASSIGNED",
        }

    @staticmethod
    def _bottom_weight(well):
        return sum(well["Weights"].get(position, 0)
                   for position in ("Bottom", "Bottom_A", "Bottom_B"))

    @staticmethod
    def _well_weight(well):
        return sum(well["Weights"].values())

    @staticmethod
    def _has_long_bottom(well):
        return well["Bottom"] is not None

    @staticmethod
    def _has_complete_twenty_foundation(well):
        return well["Bottom_A"] is not None and well["Bottom_B"] is not None

    def _validate(self, car_id, position, item, now=None):
        now = now or datetime.now(timezone.utc)
        if self.departed:
            return False, f"Train {self.train_id} has already departed"
        if now >= self.cutoff_time:
            return False, f"Loading cutoff passed for train {self.train_id}"
        if car_id not in self.wells:
            return False, f"Railcar {car_id} is not in consist"

        well = self.wells[car_id]
        length = equipment_length(item["Equipment_Type"])
        weight = int(item["Gross_Weight_Lbs"])
        destination = item["Destination_Block"]
        if weight > MAX_CONTAINER_WEIGHT_LBS:
            return False, f"Container weighs {weight:,} lb; limit is {MAX_CONTAINER_WEIGHT_LBS:,} lb"
        if length > well["Well_Length_Ft"]:
            return False, f"{length}-foot equipment does not fit a {well['Well_Length_Ft']}-foot well"
        if well["Destination_Block"] not in (None, destination):
            return False, f"Well is blocked for {well['Destination_Block']}, not {destination}"
        if self._well_weight(well) + weight > well["Max_Stack_Weight_Lbs"]:
            return False, "Combined stack weight exceeds the well limit"
        if position not in {"Bottom", "Bottom_A", "Bottom_B", "Top"}:
            return False, f"Unknown well position {position}"
        if well[position] is not None:
            return False, f"{car_id}/{position} is occupied by {well[position]}"

        if length == 20:
            if position not in {"Bottom_A", "Bottom_B"}:
                return False, "20-foot units must use a paired bottom A/B position"
            if well["Bottom"] is not None:
                return False, "A long bottom container already occupies the well"
        else:
            if position == "Bottom":
                if well["Bottom_A"] is not None or well["Bottom_B"] is not None:
                    return False, "Paired 20-foot units already occupy the bottom"
            elif position == "Top":
                foundation = self._has_long_bottom(well) or self._has_complete_twenty_foundation(well)
                if not foundation:
                    return False, "Top loading requires a complete bottom foundation"
                if weight > self._bottom_weight(well) + MAX_TOP_OVER_BOTTOM_LBS:
                    return False, "Top unit is too heavy for the bottom foundation"
            else:
                return False, f"{length}-foot equipment must use Bottom or Top"
        return True, "OK"

    def find_load_plan(self, item, now=None):
        """Return the first compatible ``(car_id, position)`` or ``None``."""
        length = equipment_length(item.get("Equipment_Type"))
        positions = ("Bottom_A", "Bottom_B") if length == 20 else ("Top", "Bottom")
        with self._lock:
            # Fill compatible tops before opening another well, then open bottoms.
            for position in positions:
                for car_id in self.wells:
                    ok, _ = self._validate(car_id, position, item, now=now)
                    if ok:
                        return car_id, position
        return None

    def can_load(self, car_id, position, container_id=None, now=None,
                 equipment_type=None, gross_weight_lbs=None, destination_block=None):
        item = self._item(
            container_id or "PREVIEW",
            equipment_type,
            gross_weight_lbs,
            destination_block,
        )
        with self._lock:
            return self._validate(car_id, position, item, now=now)

    def load_container(self, car_id, position, container_id, equipment_type=None,
                       gross_weight_lbs=None, destination_block=None):
        item = self._item(container_id, equipment_type, gross_weight_lbs, destination_block)
        with self._lock:
            ok, reason = self._validate(car_id, position, item)
            if not ok:
                raise ValueError(
                    f"Cannot load {container_id} into {car_id}/{position}: {reason}"
                )
            well = self.wells[car_id]
            well[position] = container_id
            well["Weights"][position] = item["Gross_Weight_Lbs"]
            well["Equipment"][position] = item["Equipment_Type"]
            if well["Destination_Block"] is None:
                well["Destination_Block"] = item["Destination_Block"]
            return True

    def unload_container(self, car_id, position, container_id):
        """Release an in-memory slot after a failed database transition."""
        with self._lock:
            well = self.wells.get(car_id, {})
            if well.get(position) != container_id:
                return
            well[position] = None
            well.get("Weights", {}).pop(position, None)
            well.get("Equipment", {}).pop(position, None)
            if not any(well.get(pos) for pos in ("Bottom", "Bottom_A", "Bottom_B", "Top")):
                well["Destination_Block"] = None

    def get_loaded_containers(self):
        loaded = []
        for well in self.wells.values():
            for position in ("Bottom", "Bottom_A", "Bottom_B", "Top"):
                if well[position]:
                    loaded.append(well[position])
        return loaded

    @property
    def slot_capacity(self):
        """Maximum container positions if every bottom holds paired twenties."""
        return self.well_capacity * 3

    def minutes_to_cutoff(self, now=None):
        now = now or datetime.now(timezone.utc)
        return (self.cutoff_time - now).total_seconds() / 60.0

    def depart(self, table, now=None, force=False, unsafe=False):
        now = now or datetime.now(timezone.utc)
        if self.departed or (not force and now < self.cutoff_time):
            return 0
        loaded = self.get_loaded_containers()
        if len(loaded) > 100:
            raise ValueError("Atomic departure supports at most 100 loaded containers")
        if not loaded:
            self.departed = True
            return 0

        updates = [{
            "Key": {'Container_ID': container_id},
            "UpdateExpression": (
                "set Current_Status = :s, Departure_Mode = :m, Outbound_Train_ID = :t"
            ),
            "ExpressionAttributeValues": {
                ':s': 'Departed', ':m': 'Rail', ':t': self.train_id,
            },
            "ConditionExpression": None if unsafe else Attr('Current_Status').eq('Loaded_Rail'),
        } for container_id in loaded]

        if hasattr(table, "atomic_batch_update"):
            table.atomic_batch_update(updates)
        else:
            serializer = TypeSerializer()
            serialize = lambda values: {
                key: serializer.serialize(value) for key, value in values.items()
            }
            transactions = []
            for request in updates:
                operation = {
                    "TableName": table.name,
                    "Key": serialize(request["Key"]),
                    "UpdateExpression": request["UpdateExpression"],
                    "ExpressionAttributeValues": serialize({
                        **request["ExpressionAttributeValues"],
                        **({} if unsafe else {':expected': 'Loaded_Rail'}),
                    }),
                }
                if not unsafe:
                    operation.update({
                        "ConditionExpression": "#status = :expected",
                        "ExpressionAttributeNames": {"#status": "Current_Status"},
                    })
                transactions.append({"Update": operation})
            table.meta.client.transact_write_items(TransactItems=transactions)

        self.departed = True
        return len(loaded)


_ACTIVE_TRAINS = {}


def get_outbound_train(train_id="TR-OUT-01", well_capacity=5, cutoff_minutes=60.0,
                       well_lengths=None):
    if train_id not in _ACTIVE_TRAINS:
        _ACTIVE_TRAINS[train_id] = OutboundTrain(
            train_id, well_capacity, cutoff_minutes, well_lengths=well_lengths
        )
    return _ACTIVE_TRAINS[train_id]


def reset_trains():
    _ACTIVE_TRAINS.clear()
