"""Safe online learning for railbound hostler dispatch.

The learner never decides whether a move is physically or operationally legal.
Callers first filter to valid, unclaimed railbound units; this module only chooses
which valid unit should move next.  It uses an upper-confidence-bound (UCB)
bandit to learn which of three readable dispatch rules works best:

* fifo: oldest arrival first
* nearest: shortest block-to-block move
* cutoff: least remaining target dwell first

Each completed move supplies a reward based on throughput, observed travel, and
rehandles. State is persisted as JSON so learning continues across shifts. The JSON is
small and human-readable on purpose: operators can inspect or reset it.
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


ACTIONS = ("fifo", "nearest", "cutoff")


class OnlineDispatchPolicy:
    def __init__(self, path=None, exploration=1.25, block_size=100):
        self.path = Path(path or os.environ.get("YMS_POLICY_PATH", "adaptive_policy.json"))
        self.exploration = float(exploration)
        self.block_size = int(block_size)
        self._lock = threading.Lock()
        self.counts = {action: 0 for action in ACTIONS}
        self.values = {action: 0.0 for action in ACTIONS}
        self.total_decisions = 0
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for action in ACTIONS:
                self.counts[action] = int(data.get("counts", {}).get(action, 0))
                self.values[action] = float(data.get("values", {}).get(action, 0.0))
            self.total_decisions = int(data.get("total_decisions", sum(self.counts.values())))
        except (OSError, ValueError, TypeError):
            # A damaged policy must never stop yard operations. Start clean and
            # overwrite it after the next successful observation.
            self.counts = {action: 0 for action in ACTIONS}
            self.values = {action: 0.0 for action in ACTIONS}
            self.total_decisions = 0

    @staticmethod
    def _arrival(item):
        try:
            return datetime.fromisoformat(item.get("Arrival_Time", "").replace("Z", "+00:00"))
        except ValueError:
            return datetime.max.replace(tzinfo=timezone.utc)

    def _select_action(self):
        for action in ACTIONS:
            if self.counts[action] == 0:
                return action
        log_total = math.log(max(1, self.total_decisions))
        return max(
            ACTIONS,
            key=lambda action: self.values[action]
            + self.exploration * math.sqrt(log_total / self.counts[action]),
        )

    def choose(self, candidates, current_spot=None):
        """Return ``(candidate, decision)`` for a non-empty valid candidate list."""
        if not candidates:
            return None, None
        with self._lock:
            action = self._select_action()
            self.total_decisions += 1

        if action == "nearest" and current_spot is not None:
            origin = int(current_spot) // self.block_size
            candidate = min(
                candidates,
                key=lambda item: (
                    abs((int(item.get("Assigned_Spot", 0)) // self.block_size) - origin),
                    self._arrival(item),
                ),
            )
        elif action == "cutoff":
            candidate = min(
                candidates,
                key=lambda item: (
                    float(item.get("Target_Dwell_Hours", 999999)),
                    self._arrival(item),
                ),
            )
        else:
            candidate = min(candidates, key=self._arrival)

        decision = {
            "action": action,
            "origin_spot": current_spot,
            "destination_spot": candidate.get("Assigned_Spot"),
            "container_id": candidate.get("Container_ID"),
        }
        return candidate, decision

    def observe(self, decision, completed=True):
        """Learn from a completed move and return the scalar reward.

        Throughput is valuable; block travel is a cost. A same-block dual cycle
        therefore earns more than a long empty move. Failed claims receive a
        negative reward but cannot alter any safety rule.
        """
        if not decision:
            return 0.0
        origin = decision.get("origin_spot")
        destination = decision.get("destination_spot")
        distance = decision.get("observed_block_hops")
        if distance is None:
            distance = 0
            if origin is not None and destination is not None:
                distance = abs((int(destination) // self.block_size) - (int(origin) // self.block_size))
        rehandles = int(decision.get("rehandles", 0))
        reward = (2.0 if completed else -0.5) - min(distance, 40) * 0.05 - rehandles * 0.4
        if completed and origin is not None and distance == 0 and rehandles == 0:
            reward += 0.5

        action = decision["action"]
        with self._lock:
            self.counts[action] += 1
            n = self.counts[action]
            self.values[action] += (reward - self.values[action]) / n
            self._save()
        return reward

    def _save(self):
        data = {
            "version": 1,
            "algorithm": "ucb1",
            "total_decisions": self.total_decisions,
            "counts": self.counts,
            "values": self.values,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        with self._lock:
            return {
                "algorithm": "ucb1",
                "total_decisions": self.total_decisions,
                "counts": dict(self.counts),
                "values": dict(self.values),
                "policy_file": str(self.path),
            }


_POLICY = None
_POLICY_KEY = None
_POLICY_CACHE_LOCK = threading.Lock()


def get_policy():
    global _POLICY, _POLICY_KEY
    key = (os.environ.get("YMS_POLICY_PATH", "adaptive_policy.json"),
           os.environ.get("YMS_BLOCK_SIZE", "100"))
    with _POLICY_CACHE_LOCK:
        if _POLICY is None or key != _POLICY_KEY:
            _POLICY = OnlineDispatchPolicy(path=key[0], block_size=int(key[1]))
            _POLICY_KEY = key
        return _POLICY


def reset_policy_cache():
    global _POLICY, _POLICY_KEY
    with _POLICY_CACHE_LOCK:
        _POLICY = None
        _POLICY_KEY = None
