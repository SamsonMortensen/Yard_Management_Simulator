"""An in-memory stand-in for a DynamoDB table.

Why this exists
---------------
The engines in this repo talk to a real DynamoDB table, which means running any
of them requires an AWS account, IAM credentials and a provisioned table. That
put the most interesting result in the README (the write-conflict count under
concurrent hostlers) behind a wall nobody evaluating this project is going to
climb. The number was measured once, locally, and could not be checked.

This module removes the wall. It implements the slice of the DynamoDB Table API
that `main.py`, `hostler.py`, `outgate.py` and `dispatch_check.py` actually use,
so those scripts run completely unmodified against it. `config.get_table()`
decides which backend to hand them; the engines never learn which one they got.

What it deliberately gets right
-------------------------------
Conditional writes are genuinely atomic. Every mutation takes a lock for the
read-check-write, so when two hostler threads race for the same container exactly
one wins and the loser gets a real `ConditionalCheckFailedException`. The conflict
counts this module reports are therefore the outcome of actual races between
actual threads, not a simulated number. If the lock were dropped, the simulation
would report double-parked containers, which is exactly the failure the
production design uses conditional writes to prevent.

Scans paginate. Real DynamoDB returns at most 1 MB per page and expects the
caller to follow `LastEvaluatedKey`. `config.scan_all()` handles that, but at
demo scale against real AWS the table never gets big enough to trigger it, so
that code path would never execute. Here the page size is small by default, so
every run exercises it.

What it does not do
-------------------
No GSIs, no queries, no streams, no TTL, no capacity accounting, no eventual
consistency. Note on pagination: LastEvaluatedKey returns a simplified key
cursor for in-memory iteration rather than full DynamoDB attribute descriptor
dictionaries. It is an oracle for this repo's access patterns, not a DynamoDB
reimplementation. Anything the engines here do not call is absent on purpose.

Instrumentation
---------------
The table counts its own operations: scans, items read, writes, and conflicts.
That is what lets `simulate.py` report the contention result without touching the
engine code, and it also makes the README's `Scan` cost argument measurable
rather than merely asserted.
"""
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone

from botocore.exceptions import ClientError

# Stands in for DynamoDB's 1 MB page ceiling. Small so `scan_all`'s pagination
# loop actually runs on a demo-sized table instead of always returning one page.
DEFAULT_PAGE_SIZE = 25


def _conditional_check_failed(operation):
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException",
                   "Message": "The conditional request failed"}},
        operation,
    )


# Condition evaluation ----------------------------------------------------

def _is_condition(value):
    return hasattr(value, "get_expression")


def _attr_value(item, attr):
    """Resolve an Attr against an item. Missing attributes read as absent."""
    return item.get(attr.name) if hasattr(attr, "name") else attr


def _resolve(item, value):
    return _attr_value(item, value) if hasattr(value, "name") else value


def matches(item, condition):
    """Evaluate a boto3 `Attr` condition tree against a stored item.

    Only the operators this repo's engines use are implemented, plus the boolean
    combinators. An unsupported operator raises rather than silently returning
    True: a mock that quietly matches everything would turn a broken filter
    into a passing test.
    """
    if condition is None:
        return True

    expression = condition.get_expression()
    operator = expression["operator"]
    values = expression["values"]

    if operator == "AND":
        for v in values:
            if not _is_condition(v):
                raise TypeError(f"mock_dynamo expected a condition in AND clause, got {type(v).__name__}")
            if not matches(item, v):
                return False
        return True
    if operator == "OR":
        for v in values:
            if not _is_condition(v):
                raise TypeError(f"mock_dynamo expected a condition in OR clause, got {type(v).__name__}")
            if matches(item, v):
                return True
        return False
    if operator == "NOT":
        if not _is_condition(values[0]):
            raise TypeError(f"mock_dynamo expected a condition in NOT clause, got {type(values[0]).__name__}")
        return not matches(item, values[0])

    KNOWN_OPERATORS = {
        "=", "<>", "<", "<=", ">", ">=", "BETWEEN",
        "begins_with", "contains", "IN", "attribute_exists", "attribute_not_exists"
    }
    if operator not in KNOWN_OPERATORS:
        raise NotImplementedError(
            f"mock_dynamo does not implement the {operator!r} operator. "
            "Add it rather than letting the filter silently pass."
        )

    name = values[0].name if (values and hasattr(values[0], "name")) else None
    present = name in item
    actual = item.get(name)

    if operator == "attribute_exists":
        return present
    if operator == "attribute_not_exists":
        return not present

    # Every remaining operator compares against an absent attribute as False,
    # which is how DynamoDB behaves.
    if not present:
        return False

    if operator == "=":
        return actual == _resolve(item, values[1])
    if operator == "<>":
        return actual != _resolve(item, values[1])
    if operator == "<":
        return actual < _resolve(item, values[1])
    if operator == "<=":
        return actual <= _resolve(item, values[1])
    if operator == ">":
        return actual > _resolve(item, values[1])
    if operator == ">=":
        return actual >= _resolve(item, values[1])
    if operator == "BETWEEN":
        return _resolve(item, values[1]) <= actual <= _resolve(item, values[2])
    if operator == "begins_with":
        return str(actual).startswith(str(_resolve(item, values[1])))
    if operator == "contains":
        return _resolve(item, values[1]) in actual
    if operator == "IN":
        # values[1] is the list passed to .is_in()
        return actual in values[1]

    return False


_STRING_CONDITION = re.compile(r"^\s*(attribute_not_exists|attribute_exists)\s*\(\s*(\w+)\s*\)\s*$")


def _string_condition_holds(item_exists, item, expression):
    """Evaluate the string form of ConditionExpression, e.g.
    `attribute_not_exists(Container_ID)` as used by `main.py`."""
    match = _STRING_CONDITION.match(expression)
    if not match:
        raise NotImplementedError(
            f"mock_dynamo only supports simple attribute_(not_)exists(...) string conditions; got {expression!r}"
        )
    fn, attr = match.groups()
    if fn == "attribute_not_exists":
        return not item_exists or attr not in item
    if fn == "attribute_exists":
        return item_exists and attr in item
    raise AssertionError(fn)


_SET_CLAUSE = re.compile(r"^\s*set\s+(.+)$", re.IGNORECASE)


def _apply_set_expression(item, expression, values):
    """Parse and apply `SET Current_Status = :s, Parked_By_Employee = :e`."""
    match = _SET_CLAUSE.match(expression)
    if not match:
        raise NotImplementedError(f"mock_dynamo only supports 'SET ...' updates; got {expression!r}")

    assignments = [a.strip() for a in match.group(1).split(",")]
    for assignment in assignments:
        attr, placeholder = [part.strip() for part in assignment.split("=")]
        if placeholder not in values:
            raise KeyError(f"ExpressionAttributeValues missing placeholder {placeholder!r}")
        item[attr] = deepcopy(values[placeholder])


class MockTable:
    """In-memory stand-in for boto3's Table resource."""

    def __init__(self, key_name="Container_ID", page_size=DEFAULT_PAGE_SIZE):
        self.key_name = key_name
        self.page_size = page_size
        self._items = {}  # key -> item dict
        self._events = []
        self._event_sequence = 0
        self._lock = threading.Lock()

        # Telemetry: counts operations to support simulate.py and tests
        self.stats = {
            "puts": 0,
            "put_conflicts": 0,
            "updates": 0,
            "update_conflicts": 0,
            "gets": 0,
            "scans": 0,
            "scan_pages": 0,
            "items_read_by_scans": 0,
            "queries": 0,
            "query_pages": 0,
            "items_read_by_queries": 0,
        }

    def clear(self):
        with self._lock:
            self._items.clear()
            self._events.clear()
            self._event_sequence = 0
            for k in self.stats:
                self.stats[k] = 0

    def all_items(self):
        with self._lock:
            return [deepcopy(v) for v in self._items.values()]

    def all_events(self):
        with self._lock:
            return deepcopy(self._events)

    def _record_transition(self, key_val, before, after, item):
        if before == after or after is None or str(key_val).startswith(("SPOT#", "GROUND#")):
            return
        self._event_sequence += 1
        self._events.append({
            "sequence": self._event_sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "container_id": key_val,
            "planned_departure_mode": item.get("Planned_Departure_Mode"),
            "from_status": before,
            "to_status": after,
            "handled_by": item.get("Claimed_By") or item.get("Parked_By_Employee"),
        })

    def get_item(self, Key):
        key_val = Key.get(self.key_name)
        with self._lock:
            self.stats["gets"] += 1
            item = self._items.get(key_val)
            if item is None:
                return {}
            return {"Item": deepcopy(item)}

    def delete_item(self, Key):
        key_val = Key.get(self.key_name)
        with self._lock:
            if key_val in self._items:
                del self._items[key_val]
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        key_val = Item.get(self.key_name)
        if key_val is None:
            raise ValueError(f"Item must contain primary key {self.key_name!r}")

        with self._lock:
            self.stats["puts"] += 1
            existing = self._items.get(key_val)
            item_exists = existing is not None

            if ConditionExpression is not None:
                if isinstance(ConditionExpression, str):
                    holds = _string_condition_holds(item_exists, existing or {}, ConditionExpression)
                else:
                    holds = matches(existing or {}, ConditionExpression)
                if not holds:
                    self.stats["put_conflicts"] += 1
                    raise _conditional_check_failed("PutItem")

            self._items[key_val] = deepcopy(Item)
            self._record_transition(key_val, None, Item.get("Current_Status"), Item)
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ConditionExpression=None):
        key_val = Key.get(self.key_name)
        if key_val is None:
            raise ValueError(f"Key must contain primary key {self.key_name!r}")

        with self._lock:
            self.stats["updates"] += 1
            item = self._items.get(key_val)
            item_exists = item is not None

            if not item_exists:
                self.stats["update_conflicts"] += 1
                raise _conditional_check_failed("UpdateItem")

            if ConditionExpression is not None:
                if isinstance(ConditionExpression, str):
                    holds = _string_condition_holds(True, item, ConditionExpression)
                else:
                    holds = matches(item, ConditionExpression)
                if not holds:
                    self.stats["update_conflicts"] += 1
                    raise _conditional_check_failed("UpdateItem")

            updated = deepcopy(item)
            _apply_set_expression(updated, UpdateExpression, ExpressionAttributeValues)
            if ExpressionAttributeValues and "Parked" in ExpressionAttributeValues.values():
                self.stats["successful_parks"] = self.stats.get("successful_parks", 0) + 1
            self._items[key_val] = updated
            self._record_transition(
                key_val, item.get("Current_Status"), updated.get("Current_Status"), updated
            )
            return {"Attributes": deepcopy(updated),
                    "ResponseMetadata": {"HTTPStatusCode": 200}}

    def atomic_update_and_delete(self, Key, UpdateExpression,
                                 ExpressionAttributeValues, ConditionExpression,
                                 DeleteKey):
        """Mock DynamoDB transaction used when a move frees a ground slot."""
        key_val = Key.get(self.key_name)
        delete_val = DeleteKey.get(self.key_name)
        with self._lock:
            self.stats["updates"] += 1
            item = self._items.get(key_val)
            if item is None or not matches(item, ConditionExpression):
                self.stats["update_conflicts"] += 1
                raise _conditional_check_failed("TransactWriteItems")
            updated = deepcopy(item)
            _apply_set_expression(updated, UpdateExpression, ExpressionAttributeValues)
            if "Parked" in ExpressionAttributeValues.values():
                self.stats["successful_parks"] = self.stats.get("successful_parks", 0) + 1
            self._items[key_val] = updated
            self._items.pop(delete_val, None)
            self._record_transition(
                key_val, item.get("Current_Status"), updated.get("Current_Status"), updated
            )
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def atomic_batch_update(self, updates):
        """Validate and apply all train departure updates under one lock."""
        with self._lock:
            prepared = []
            for request in updates:
                key_val = request["Key"].get(self.key_name)
                item = self._items.get(key_val)
                if item is None or not matches(item, request.get("ConditionExpression")):
                    self.stats["update_conflicts"] += 1
                    raise _conditional_check_failed("TransactWriteItems")
                updated = deepcopy(item)
                _apply_set_expression(
                    updated,
                    request["UpdateExpression"],
                    request["ExpressionAttributeValues"],
                )
                prepared.append((key_val, item, updated))
            self.stats["updates"] += len(prepared)
            for key_val, item, updated in prepared:
                self._items[key_val] = updated
                self._record_transition(
                    key_val, item.get("Current_Status"), updated.get("Current_Status"), updated
                )
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def atomic_relocate(self, Key, UpdateExpression, ExpressionAttributeValues,
                        ConditionExpression, OldReservationKey, NewReservationItem):
        """Atomically move a parked blocker and exchange its ground reservation."""
        key_val = Key[self.key_name]
        old_key = OldReservationKey[self.key_name]
        new_key = NewReservationItem[self.key_name]
        with self._lock:
            item = self._items.get(key_val)
            if item is None or not matches(item, ConditionExpression) or new_key in self._items:
                self.stats["update_conflicts"] += 1
                raise _conditional_check_failed("TransactWriteItems")
            updated = deepcopy(item)
            _apply_set_expression(updated, UpdateExpression, ExpressionAttributeValues)
            self.stats["updates"] += 1
            self.stats["puts"] += 1
            self._items[new_key] = deepcopy(NewReservationItem)
            self._items[key_val] = updated
            self._items.pop(old_key, None)
            self._event_sequence += 1
            self._events.append({
                "sequence": self._event_sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "container_id": key_val,
                "event_type": "Ground_Rehandle",
                "from_location": old_key,
                "to_location": new_key,
                "handled_by": ExpressionAttributeValues.get(':employee'),
            })
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def scan(self, FilterExpression=None, ProjectionExpression=None,
             ExclusiveStartKey=None, Limit=None):
        limit = Limit or self.page_size

        with self._lock:
            if ExclusiveStartKey is None:
                self.stats["scans"] += 1
            self.stats["scan_pages"] += 1

            all_keys = sorted(self._items.keys())
            start_index = 0
            if ExclusiveStartKey is not None:
                # Accept a dict or raw key
                cursor = ExclusiveStartKey.get(self.key_name) if isinstance(ExclusiveStartKey, dict) else ExclusiveStartKey
                import bisect
                start_index = bisect.bisect_right(all_keys, cursor)

            page_keys = all_keys[start_index:start_index + limit]
            self.stats["items_read_by_scans"] += len(page_keys)

            matched_items = []
            for k in page_keys:
                item = self._items[k]
                if matches(item, FilterExpression):
                    if ProjectionExpression:
                        fields = [f.strip() for f in ProjectionExpression.split(",")]
                        projected = {f: item[f] for f in fields if f in item}
                        matched_items.append(projected)
                    else:
                        matched_items.append(deepcopy(item))

            result = {"Items": matched_items, "Count": len(matched_items)}
            if start_index + limit < len(all_keys):
                result["LastEvaluatedKey"] = {self.key_name: page_keys[-1]}
            return result


    def query(self, IndexName=None, KeyConditionExpression=None, FilterExpression=None, Limit=None, ExclusiveStartKey=None):
        limit = Limit or self.page_size
        
        with self._lock:
            if ExclusiveStartKey is None:
                self.stats["queries"] += 1
            self.stats["query_pages"] += 1

            # Determine matching items for the GSI partition key
            all_matching = []
            if IndexName == 'StatusIndex' and KeyConditionExpression:
                # Naive in-memory filter matching KeyConditionExpression exactly
                for item in self._items.values():
                    if matches(item, KeyConditionExpression):
                        all_matching.append(item)
            else:
                raise NotImplementedError("mock_dynamo only supports querying StatusIndex with KeyConditionExpression")
            
            # Sort by primary key for deterministic pagination
            all_matching.sort(key=lambda x: x[self.key_name])
            
            start_index = 0
            if ExclusiveStartKey is not None:
                cursor = ExclusiveStartKey.get(self.key_name) if isinstance(ExclusiveStartKey, dict) else ExclusiveStartKey
                for i, item in enumerate(all_matching):
                    if item[self.key_name] > cursor:
                        start_index = i
                        break
                else:
                    start_index = len(all_matching)

            page_items = all_matching[start_index:start_index + limit]
            
            # Here is the magic of the GSI: it only reads exactly what matched the partition key!
            self.stats["items_read_by_queries"] += len(page_items)

            # Apply FilterExpression if any (post-filtering on the matched partition)
            final_items = []
            from copy import deepcopy
            for item in page_items:
                if FilterExpression is None or matches(item, FilterExpression):
                    final_items.append(deepcopy(item))

            result = {"Items": final_items, "Count": len(final_items)}
            if start_index + limit < len(all_matching):
                result["LastEvaluatedKey"] = {self.key_name: page_items[-1][self.key_name]}
            return result

# Singleton instance shared by the engines during simulate.py
_SHARED = MockTable()


def shared_table():
    return _SHARED


def reset_shared_table(page_size=DEFAULT_PAGE_SIZE):
    _SHARED.key_name = "Container_ID"
    _SHARED.page_size = page_size
    _SHARED.clear()
    return _SHARED
