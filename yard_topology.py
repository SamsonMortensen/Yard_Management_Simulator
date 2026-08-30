"""Physical ground layout, multi-tier stacks, travel, and rehandles."""
from __future__ import annotations

import os
from collections import defaultdict

import config
from flow import flow_label


GROUND_PREFIX = "GROUND#"


def max_tiers():
    return int(os.environ.get("YMS_MAX_TIERS", "3"))


def block_size():
    return int(os.environ.get("YMS_BLOCK_SIZE", "100"))


def yard_block(spot):
    return int(spot) // block_size()


def gate_block():
    return config.MAX_SPOT // block_size()


def track_block():
    return config.MIN_SPOT // block_size()


def reservation_id(spot, tier):
    return f"{GROUND_PREFIX}{int(spot)}#{int(tier)}"


def is_reservation_id(container_id):
    return str(container_id).startswith((GROUND_PREFIX, "SPOT#"))


def parse_reservation(item):
    cid = item.get("Container_ID", "")
    if cid.startswith(GROUND_PREFIX):
        _, spot, tier = cid.split("#")
        return int(spot), int(tier)
    if cid.startswith("SPOT#"):
        return int(cid.split("#")[1]), 1
    return None


def _container_items(items):
    return [item for item in items if not is_reservation_id(item.get("Container_ID", ""))]


def choose_ground_location(items, container, exclude_spots=None):
    """Choose a readable block/bay/tier location using dwell-aware stacking.

    Units with shorter target dwell are favored above longer-dwell units so the
    next pickup is less likely to be buried. Same-flow stacks and railbound
    destination blocks are preferred, but capacity is never fabricated.
    """
    exclude_spots = set(exclude_spots or ())
    reservations = defaultdict(set)
    for item in items:
        parsed = parse_reservation(item)
        if parsed:
            reservations[parsed[0]].add(parsed[1])

    containers_by_spot = defaultdict(list)
    for item in _container_items(items):
        if item.get("Current_Status") in {"Parked", "Claimed", "Ingate_Hold", "Buffer_Hold", "Rendezvous_Wait", "Trackside_Hold"}:
            if item.get("Assigned_Spot") is not None:
                containers_by_spot[int(item["Assigned_Spot"])].append(item)

    target_dwell = float(container.get("Target_Dwell_Hours", 999999))
    source_block = track_block() if container.get("Arrival_Mode") == "Rail" else gate_block()
    desired_flow = flow_label(container)
    desired_destination = container.get("Destination_Block", "UNASSIGNED")
    candidates = []

    for spot in range(config.MIN_SPOT, config.MAX_SPOT + 1):
        if spot in exclude_spots:
            continue
        tiers = reservations.get(spot, set())
        if len(tiers) >= max_tiers():
            continue
        tier = 1 if not tiers else max(tiers) + 1
        if tier > max_tiers() or any(t not in tiers for t in range(1, tier)):
            continue

        stack = containers_by_spot.get(spot, [])
        flow_penalty = 0 if not stack or all(flow_label(i) == desired_flow for i in stack) else 8
        destination_penalty = 0
        if desired_flow == "Railbound" and stack:
            destination_penalty = 0 if all(
                i.get("Destination_Block", "UNASSIGNED") == desired_destination for i in stack
            ) else 12
        # Putting a long-dwell unit above a short-dwell unit creates a likely dig.
        dwell_penalty = sum(
            max(0.0, target_dwell - float(i.get("Target_Dwell_Hours", target_dwell))) / 12.0
            for i in stack
        )
        travel = abs(yard_block(spot) - source_block)
        new_stack_penalty = 1.5 if not stack else 0.0
        candidates.append((
            flow_penalty + destination_penalty + dwell_penalty + travel * 0.15 + new_stack_penalty,
            tier,
            spot,
        ))

    if not candidates:
        raise RuntimeError("Yard is full: no ground stack tier is available")
    _, tier, spot = min(candidates)
    return {
        "Assigned_Spot": spot,
        "Ground_Tier": tier,
        "Yard_Block": yard_block(spot),
        "Ground_Reservation_ID": reservation_id(spot, tier),
    }


def blocking_containers(table, target):
    """Return physical blockers above target, topmost first."""
    spot = int(target.get("Assigned_Spot", -1))
    tier = int(target.get("Ground_Tier", 1))
    if hasattr(table, "all_items"):
        items = table.all_items()
    else:
        from config import scan_all
        items = scan_all(table)
    blockers = [
        item for item in _container_items(items)
        if int(item.get("Assigned_Spot", -2)) == spot
        and int(item.get("Ground_Tier", 1)) > tier
        and item.get("Current_Status") in {"Parked", "Claimed"}
    ]
    return sorted(blockers, key=lambda item: int(item.get("Ground_Tier", 1)), reverse=True)


def block_hops(origin_spot, destination_block):
    return abs(yard_block(origin_spot) - int(destination_block))


def rehandle_for_access(table, target, employee):
    """Relocate every blocker above ``target`` and return movement telemetry."""
    from atomic_ops import relocate_ground_unit
    from config import scan_all

    blockers = blocking_containers(table, target)
    total_hops = 0
    for blocker in blockers:
        items = table.all_items() if hasattr(table, "all_items") else scan_all(table)
        location = choose_ground_location(
            items,
            blocker,
            exclude_spots={int(target['Assigned_Spot'])},
        )
        total_hops += abs(
            yard_block(blocker['Assigned_Spot']) - location['Yard_Block']
        )
        relocate_ground_unit(table, blocker, location, employee)
    return {"rehandles": len(blockers), "block_hops": total_hops}
