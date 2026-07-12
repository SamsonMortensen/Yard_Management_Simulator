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
6. Run the engine scripts (`main.py`, `hostler.py`, and `outgate.py`) in separate terminals.


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

* **Sample session (simulated):** [N] containers ingated across a shift, parked by two
concurrent hostler processes with **zero double-parks** ([X] write conflicts detected and
resolved by conditional writes), [K] departures logged, average dwell [H] hours, and the
TAS denied [D] dry-run attempts.


## Future Roadmap & Suggested Enhancements

......
