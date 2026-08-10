# Serverless Intermodal Yard Management System (YMS) Simulator

## Overview

This event-driven, microservice-based setup simulates real-time intermodal logistics and yard operations. It replicates the journey of freight containers through a transit facility—from gate arrival, to hostler parking, all the way to the final outgate—while tracking live operational metrics and ensuring data governance.

## The Business Problem

Supply chains often experience delays between actual yard movements and updates to enterprise databases, leading to issues such as dry runs, misparks, and inefficient capacity planning. Our project tackles this by leveraging cloud infrastructure and real-time data pipelines, giving dispatchers quick, actionable insights and securely logging employee actions.

## Architecture & Tech Stack

* **Cloud Database:** AWS DynamoDB (NoSQL) for fast, key-value status updates.

* **Simulation Engine:** Python (`boto3`) scripts that simulate gate clerks, yard hostlers, and outbound dispatchers.

* **Analytics Front-End:** Streamlit & Pandas for live data visualization and KPI monitoring.

## Core Features

* **Live Ingestion Pipeline:** Creates random inbound container traffic and assigns numeric parking spots to keep things running smoothly.

* **Automated State Changes:** Background processes update container statuses automatically (`Ingate_Hold` → `Parked` → `Departed`).

* **Terminal Appointment System (TAS):** Smart business logic prevents "Dry Runs" by checking container availability before issuing gate codes.

* **Internal Audit Trailing:** Logs unique employee IDs linked to yard movements in the cloud, keeping audit data separate from the public dispatch dashboard.

* **Real-Time Analytics:** Continuously calculates and shows yard capacity usage and container dwell times.

## How to Run Locally  

1. Clone the repo.
2. Install dependencies with `pip install -r requirements.txt`.
3. Set up your AWS CLI with the right IAM credentials (`aws configure`) for DynamoDB access.
4. Create the table: `python setup_table.py`.
5. Start the dashboard using `streamlit run app.py`.
6. Run the engine scripts in separate terminals:
   - `python main.py` drops units at the gate (pass a count, e.g. `python main.py 20`).
   - `python hostler.py` works the gate queue until it's clear, then clocks out.
   - `python outgate.py` pulls parked units off the ground until the yard is empty, then clocks out.
7. Check a unit against the TAS with `python dispatch_check.py`. Grab a `Container_ID` off the dashboard roster when it asks.


## Concurrency & Scaling Considerations

**Optimistic concurrency.** Multiple hostler and outgate processes poll the same table, so two
workers can target the same container. Every state transition uses a DynamoDB conditional write
(`ConditionExpression` on the current status): the transition is atomic, exactly one writer
succeeds, and the loser catches `ConditionalCheckFailedException` and rescans. No locks, no
coordinator process.

**Scan vs. Query.** The engines poll with `Scan` + `FilterExpression`, which reads the entire
table and filters afterward — fine at demo scale, but read cost grows linearly with table size.
The production design is a Global Secondary Index keyed on `Current_Status`, letting each engine
`Query` only the items in the state it cares about. The scans are kept here to keep the demo to
a single table; the GSI refactor is on the roadmap.

**Pagination.** DynamoDB returns at most 1 MB per scan page. All scans go through a shared
helper that follows `LastEvaluatedKey`, so results stay complete no matter how large the
table grows.


## Simulated Business Outcomes

By implementing this cloud-native architecture, terminal operators can expect to see several key improvements:

* **Elimination of Dry Runs:** The Terminal Appointment System (TAS) successfully intercepts and denies gate access for units not physically grounded, saving drayage drivers hours of wasted time and reducing gate-lane congestion.

* **Granular Accountability:** Decoupling the public dispatch view from the internal AWS database ensures that every physical yard move is permanently tied to a specific hostler (e.g., EMP-309), providing management with an immutable audit trail for damage claims or misparks.

* **Real-Time Capacity Visibility:** Transitioning from batch-processed spreadsheets to an event-driven DynamoDB pipeline reduces visibility latency to near-zero, allowing dispatchers to accurately gauge yard utilization and average dwell times by the minute.

* **Sample session (simulated):** 60 containers ingated across one shift and parked by two
concurrent hostler processes drawing from a four-driver roster, with **59 write conflicts
detected and resolved by conditional writes**. All 60 units were grounded exactly once, 60
departures were logged with an average dwell of 4.3 seconds of wall clock (the simulator
compresses a shift into seconds, so treat dwell as a plumbing check rather than an
operational figure), and the TAS denied 12 of 12 dry-run attempts against units that were
not on the ground.

  Measured by running the engine scripts unmodified against a mocked DynamoDB, so the
conflict count is the outcome of real races rather than an estimate. That 59 out of 60 figure
is worth reading carefully: it is not noise, it is the contention ceiling. Every hostler
claims `gate_items[0]`, so two processes always drive to the same container, and one always
loses. The conditional write does its job — no unit was ever double-parked — but the losing
process burns a round trip each time. Letting hostlers draw from anywhere in the waiting
queue instead of the head drops the same workload from 59 conflicts to 3. That change is
deliberately not made here, because taking units out of arrival order abandons FIFO, and in a
real yard the first truck in line is the one that has been waiting longest. Fixing contention
properly means a dispatcher assigning moves rather than workers racing for them — see the
roadmap.


## Future Roadmap & Suggested Enhancements

Four known limits in the current build, in the order I would fix them:

* **Move the status lookups off `Scan`.** The engines scan the whole table and filter for `Ingate_Hold` or `Parked` afterward, so every pass reads — and pays for — every row, including units that outgated weeks ago. A Global Secondary Index on `Current_Status` turns each of those into a `Query` that only touches the units the worker actually wants. `scan_all()` handles pagination correctly in the meantime so nothing is silently dropped, but the read cost grows linearly with the table and this is the first thing that breaks at real volume.

* **Dispatch moves instead of letting hostlers race for them.** As measured above, two hostlers both claim the head of the gate queue and one always loses the conditional write. Correctness holds, but roughly half the fleet's round trips are wasted. The fix is a dispatcher that assigns a unit to a specific hostler — a `Claimed_By` stamp written before the drive rather than after it — which preserves FIFO and removes the contention instead of just surviving it.

* **Build a real audit trail on DynamoDB Streams.** `Parked_By_Employee` is stamped onto the container record, so a later move overwrites the earlier one and only the most recent hostler is on file. Piping the table's stream into an append-only move log gives management the full chain of custody a damage claim actually needs.

* **Add a TTL or archive step for departed units.** Outgated containers stay in the table forever. They no longer count against yard capacity, but they pad every scan and every read.
