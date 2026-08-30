"""Per-container lifecycle auditing for the in-memory simulation."""

from flow import planned_departure_mode
from yard_topology import is_reservation_id


ALLOWED = {
    "Road": {
        "Trackside_Hold": {"Claimed", "Buffer_Hold", "Rendezvous_Wait"},
        "Claimed": {"Buffer_Hold", "Rendezvous_Wait", "Parked", "Awaiting_Rail", "Loaded_Rail", "Departed"},
        "Buffer_Hold": {"Claimed", "Parked"},
        "Rendezvous_Wait": {"Claimed", "Parked"},
        "Parked": {"Claimed", "Departed"},
        "Departed": set(),
    },
    "Rail": {
        "Ingate_Hold": {"Claimed", "Parked"},
        "Claimed": {"Parked", "Awaiting_Rail", "Loaded_Rail"},
        "Parked": {"Claimed", "Awaiting_Rail"},
        "Awaiting_Rail": {"Claimed", "Loaded_Rail"},
        "Loaded_Rail": {"Departed"},
        "Departed": set(),
    },
}


def audit_lifecycles(events, items):
    """Prove valid ordered transitions per container, not by aggregate counts."""
    containers = {
        item["Container_ID"]: item
        for item in items
        if not is_reservation_id(item.get("Container_ID", ""))
    }
    problems = []
    by_container = {cid: [] for cid in containers}
    for event in events:
        cid = event.get("container_id")
        if cid in by_container:
            by_container[cid].append(event)

    for cid, item in containers.items():
        departure_mode = planned_departure_mode(item)
        allowed = ALLOWED.get(departure_mode)
        history = by_container[cid]
        if allowed is None:
            problems.append(f"{cid}: unknown Planned_Departure_Mode={departure_mode}")
            continue
        if not history:
            problems.append(f"{cid}: no lifecycle events recorded")
            continue
        for event in history:
            if event.get("to_status") is None:
                continue
            before, after = event.get("from_status"), event.get("to_status")
            if before is None:  # creation event
                expected = "Trackside_Hold" if departure_mode == "Road" else "Ingate_Hold"
                if after not in (expected, "Parked"):  # seeded inventory may start parked
                    problems.append(f"{cid}: created as {after}, expected {expected}")
            elif after not in allowed.get(before, set()):
                problems.append(f"{cid}: illegal transition {before}->{after}")

        departed_events = [e for e in history if e.get("to_status") == "Departed"]
        if len(departed_events) > 1:
            problems.append(f"{cid}: departed {len(departed_events)} times")
        final_status = item.get("Current_Status")
        valid_final = {"Departed"} if departure_mode == "Road" else {"Departed", "Awaiting_Rail"}
        if final_status not in valid_final:
            problems.append(f"{cid}: unexpected final status {final_status}")

    return {"passed": not problems, "problems": problems, "containers_checked": len(containers)}
