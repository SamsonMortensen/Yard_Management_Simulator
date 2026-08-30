# Changelog

## v0.2.0: Physical Yard and Adaptive Dispatch

This release connects the yard's database state to physical ground and train constraints.
It is the first public portfolio release.

## Physical yard and train planning

- Added configurable yard blocks and multi-tier ground stacks with exact
  `GROUND#spot#tier` reservations.
- Added dwell-aware placement that favors compatible flows and rail destination
  blocks without burying short-dwell work under long-dwell work.
- Added atomic blocker relocation before a buried container can move.
- Added block-hop, rehandle, and simulated operating-time telemetry.
- Added 40- and 53-foot well compatibility, paired 20-foot bottom positions,
  container and stack weight limits, foundation rules, and destination blocking.
- The outbound crane now selects and reserves a physically valid railcar position
  immediately before it commits the database transition.

## Terminal language

- Replaced ambiguous operational use of import/export with **railbound** and
  **roadbound**.
- Added canonical `Planned_Departure_Mode` (`Rail` or `Road`).
- Kept automatic compatibility with older `Direction=Export|Import` manifests.

## Operational correctness

- Bottom containers now reference the actual blocking top container ID.
- Roadbound units cannot outgate until target dwell has elapsed.
- Train cutoff is enforced and loading closes at cutoff.
- Configurable well capacity can bind; railbound units left behind are counted
  as rollovers.
- Well slots are reserved before the database commits a crane load, closing a
  multi-crane race.
- Train departure is transactional for up to 100 loaded units.
- Container transition plus ground-slot release is transactional.
- Dispatch candidates are explicitly sorted by arrival time.
- Yard block size is configurable.

## Verification

- Replaced aggregate write-count arithmetic with an ordered, per-container
  lifecycle ledger and validator.
- Command-line simulation now exits nonzero when lifecycle or TAS validation
  fails.
- Expanded validation to 29 pytest tests and 77 readable assertions.
- Added cutoff, atomic departure, dwell, stack blocker, rollover, and adaptive
  policy persistence tests.

## Continuous online learning

- Added a persistent UCB online dispatch learner.
- It learns among FIFO, nearest-block, and cutoff-priority retrieval rules.
- It updates after observed moves, including actual block hops and rehandles, and
  continues from a human-readable JSON
  policy on the next shift.
- It can rank only valid, unclaimed railbound work. Hard status, stack, train,
  capacity, and cutoff constraints remain outside ML control.
- Added adaptive-policy telemetry to the CLI and Streamlit dashboard.
- Added a reproducible scale experiment with matched seeds and fixed baselines.
- Published 44 measured shifts at 100, 200, and 400 units without overstating the
  current learner's performance.
- Made policy-cache initialization thread-safe under concurrent hostlers.
- Added reservation identity to retrieval and blocker-relocation transactions, closing
  stale-location races found only under larger runs.

## Next release

- Model the track dimensions.
- Expand physical track and train planning.
- Continue reproducible multi-shift evaluation against fixed policies.
