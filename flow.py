"""Plain-language container flow terminology.

Railbound: enters through the road gate and departs on an outbound train.
Roadbound: arrives on an inbound train and departs through the road outgate.

``Planned_Departure_Mode`` is the canonical field. ``Direction`` remains a
read-only compatibility fallback for manifests created by earlier versions.
"""


def planned_departure_mode(item):
    explicit = item.get("Planned_Departure_Mode")
    if explicit in {"Rail", "Road"}:
        return explicit
    return "Rail" if item.get("Direction") == "Export" else "Road"


def is_railbound(item):
    return planned_departure_mode(item) == "Rail"


def is_roadbound(item):
    return planned_departure_mode(item) == "Road"


def flow_label(item):
    return "Railbound" if is_railbound(item) else "Roadbound"
