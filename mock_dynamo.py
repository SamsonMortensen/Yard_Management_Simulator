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
        return actual in [_resolve(item, v) for v in values[1:]]

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
        }

    def clear(self):
        with self._lock:
            self._items.clear()
            for k in self.stats:
                self.stats[k] = 0

    def all_items(self):
        with self._lock:
            return [deepcopy(v) for v in self._items.values()]

    def get_item(self, Key):
        key_val = Key.get(self.key_name)
        with self._lock:
            self.stats["gets"] += 1
            item = self._items.get(key_val)
            if item is None:
                return {}
            return {"Item": deepcopy(item)}

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
            return {"Attributes": deepcopy(updated),
                    "ResponseMetadata": {"HTTPStatusCode": 200}}

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
                if cursor in all_keys:
                    start_index = all_keys.index(cursor) + 1

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


# Singleton instance shared by the engines during simulate.py
_SHARED = MockTable()


def shared_table():
    return _SHARED


def reset_shared_table(page_size=DEFAULT_PAGE_SIZE):
    _SHARED.key_name = "Container_ID"
    _SHARED.page_size = page_size
    _SHARED.clear()
    return _SHARED
