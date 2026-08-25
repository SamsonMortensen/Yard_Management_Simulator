# Serverless Intermodal Yard Management System (YMS) Simulator

## Overview

This event-driven, microservice-based setup simulates real-time intermodal logistics and yard operations. It replicates the journey of freight containers through a transit facility from gate arrival to hostler parking, all the way to the final outgate, while tracking live operational metrics and ensuring data governance.

## The Business Problem

Supply chains often experience delays between actual yard movements and updates to enterprise databases, leading to issues such as dry runs, misparks, and inefficient capacity planning. Our project tackles this by leveraging cloud infrastructure and real-time data pipelines, giving dispatchers quick, actionable insights and securely logging employee actions.

## Architecture & Tech Stack

* **Cloud Database:** AWS DynamoDB (NoSQL) for fast, key-value status updates.
* **Simulation Engine:** Python (`boto3`) scripts that simulate gate clerks, yard hostlers, and outbound dispatchers.
* **Analytics Front-End:** Streamlit & Pandas for live data visualization and KPI monitoring.

## Core Features

* **Live Ingestion Pipeline:** Creates random inbound container traffic and assigns numeric parking spots to keep things running smoothly.
* **Automated State Changes:** Background processes update container statuses automatically (`Ingate_Hold` -> `Parked` -> `Departed`).
* **Terminal Appointment System (TAS):** Smart business logic prevents "Dry Runs" by checking container availability before issuing gate codes.
* **Internal Audit Trailing:** Logs unique employee IDs linked to yard movements in the cloud, keeping audit data separate from the public dispatch dashboard.
* **Real-Time Analytics:** Continuously calculates and shows yard capacity usage and container dwell times.

## Run it in two commands, with no AWS account

```bash
pip install -r requirements.txt
python simulate.py
```

That runs a full shift: 60 containers ingated, parked by two concurrent hostlers, then outgated, against an in-memory DynamoDB stand-in (`mock_dynamo.py`), and prints the correctness checks, the write-contention count and the scan cost.

The engines are **not modified** to make this work. `main.py`, `hostler.py`, `outgate.py` and `dispatch_check.py` run exactly as they would against real DynamoDB; `config.get_table()` simply hands them a different table object. Every conditional write, every retry and every filter is the real one. Only the simulated drive-time sleeps are compressed, via `--speed`.

Useful flags:

```bash
python simulate.py --compare                   # run side-by-side benchmark of control vs guarded modes
python simulate.py --unsafe                    # run without conditional writes to prove failure mode
python simulate.py --repeat 10                 # report distribution across 10 shift runs
python simulate.py --claim dispatch            # run with centralized task assignment (0 conflicts + FIFO)
python simulate.py --export-csv shift.csv      # dump shift telemetry to CSV for pandas analytics
python test_yard.py                            # 48 unit and integration tests
```

## How to Run Against Real AWS  

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

**Optimistic concurrency.** Multiple hostler and outgate processes poll the same table, so two workers can target the same container. Every state transition uses a DynamoDB conditional write (`ConditionExpression` on the current status): the transition is atomic, exactly one writer succeeds, and the loser catches `ConditionalCheckFailedException` and rescans. No locks, no coordinator process.

**Scan vs. Query.** The engines poll with `Scan` + `FilterExpression`, which reads the entire table and filters afterward (fine at demo scale, but read cost grows linearly with table size). The production design is a Global Secondary Index keyed on `Current_Status`, letting each engine `Query` only the items in the state it cares about. The scans are kept here to keep the demo to a single table; the GSI refactor is on the roadmap.

**Pagination.** DynamoDB returns at most 1 MB per scan page. All scans go through a shared helper that follows `LastEvaluatedKey`, so results stay complete no matter how large the table grows.


## Measured Findings: Proving the Failure Mode

A concurrency design is only as credible as the failure it prevents. To prove that DynamoDB conditional writes are load-bearing, the simulator includes an uncoordinated control mode (`--unsafe`) that runs the identical 60-container shift with conditional writes removed.

**Empirical Benchmark (reproduce with `python simulate.py --compare`):**
State space guarantees across all runs for a 60-container shift with 2 hostlers and 1 outgate:

| Operational Mode | FIFO Preserved? | Successful Park Writes | Duplicate Misparks | Conflicts Intercepted |
| :--- | :---: | :---: | :---: | :---: |
| **Unsafe Blind Writes (`--unsafe`)** | **Yes** | **> 60** (Expected: 60) | **> 0** | **0 (Blind Overwrite)** |
| **Guarded FIFO (`--claim head`)** | **Yes** | **Exactly 60** | **Exactly 0** | **<= 60 (Misparks Prevented)** |
| **Random Draw (`--claim random`)** | **No** | **Exactly 60** | **Exactly 0** | **< 60 (Reduced Contention)** |
| **Centralized Dispatch (`--claim dispatch`)** | **Yes** | **Exactly 60** | **Exactly 0** | **0 (0 Conflicts)** |

*For exact numeric distributions from a recorded run, see [benchmark.txt](benchmark.txt) (recorded on Python 3.14 / Windows).*

### Analytical Breakdown:

1. **The Unsafe Control Proves Causality:** Without conditional writes, both hostlers read the queue, drive simultaneously, and blindly overwrite container records. Across 60 containers, this executes more than 60 park writes and leaves multiple containers double-parked with split-brain employee logs. Zero database conflicts are raised, but data integrity is silently broken.
2. **Reframing Write Contention:** Under Guarded FIFO, the detected conflict count is bounded by [0, 60] and is not mere overhead: **it is the exact count of physical misparks intercepted and prevented by the database**. 60 is the mathematical contention ceiling (one lost race per container under 2 concurrent workers). Thread timing causes variance across machines, but correctness holds across 100% of runs.
3. **Queue Strategy Optimization:**
   - **Random Draw (`--claim random`)** breaks FIFO arrival order but scales much better: FIFO conflicts scale with the container count (ceiling = N, one lost race per unit), while random-draw conflicts stay near-constant as N grows.
   - **Centralized Dispatch (`--claim dispatch`)** moves contention off the expensive physical drive path onto the in-memory claim retry, achieving 0 parking collisions while preserving strict customer arrival order.

**Scan cost, measured:** A filtered scan reads every row in the table and pays for every row, regardless of how many match. That is the entire argument for the Global Secondary Index at the top of the roadmap, and `test_scan_counts_rows_read_before_filtering` proves it deterministically by reading 40 rows to return exactly 1. Total shift scan counts are recorded alongside the benchmarking output in `benchmark.txt`.


## Future Roadmap & Suggested Enhancements

Four known limits in the current build, in the order I would fix them:

* **Move the status lookups off `Scan`.** The engines scan the whole table and filter for `Ingate_Hold` or `Parked` afterward, so every pass reads, and pays for, every row, including units that outgated weeks ago. A Global Secondary Index on `Current_Status` turns each of those into a `Query` that only touches the units the worker actually wants. `scan_all()` handles pagination correctly in the meantime so nothing is silently dropped, but the read cost grows linearly with the table and this is the first thing that breaks at real volume.

* **Build a real audit trail on DynamoDB Streams.** `Parked_By_Employee` is stamped onto the container record, so a later move overwrites the earlier one and only the most recent hostler is on file. Piping the table's stream into an append-only move log gives management the full chain of custody a damage claim actually needs.

* **Add a TTL or archive step for departed units.** Outgated containers stay in the table forever. They no longer count against yard capacity, but they pad every scan and every read.
