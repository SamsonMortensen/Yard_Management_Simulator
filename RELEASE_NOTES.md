# v0.2.0: Physical Yard and Adaptive Dispatch

This is the first version I consider ready to present as a complete portfolio release.
It combines my experience working intermodal operations with the data engineering and
analytics work I studied in my bachelor's program.

## What changed

- Replaced operational import and export labels with railbound and roadbound.
- Added configurable yard blocks and multi-tier ground stacks.
- Added exact ground reservations by spot and tier.
- Added blocker rehandles before a buried unit can be retrieved.
- Added measured block travel, rehandles, dwell, and simulated work time.
- Added equipment-aware outbound wells with length, weight, foundation, and destination
  constraints.
- Added train cutoff, rollover, slot reservation, and atomic departure behavior.
- Added a persistent online UCB learner for valid hostler dispatch choices.
- Added ordered lifecycle auditing and expanded automated verification.
- Updated the dashboard, historical manifest builder, documentation, and CI workflow.

## Verification

- 29 pytest tests pass.
- 77 narrated assertions pass.
- Guarded simulations validate every container lifecycle.
- Ground reservations remain consistent under concurrent work.
- Adaptive runs persist learning between shifts without bypassing operational rules.
- A published 44-shift scale experiment covers 100, 200, and 400-unit workloads.
- Scale testing closed stale retrieval, duplicate relocation, and concurrent policy-cache
  initialization races before release.

## Scope

This is a portfolio and research simulator, not production terminal-management software.
The train weight rules are operating approximations, chassis are currently unlimited, and
the physical track layout is not active yet.

Future work will expand the physical track and train-planning model without changing the
correctness boundaries established in this release.
