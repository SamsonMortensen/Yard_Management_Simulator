# Intermodal Railyard Simulator

A working model of an intermodal rail terminal: trains discharging on the track, cranes
working double stack wells, hostlers moving units around the yard, and outside drivers
coming through the gate to drop off and pick up.

I spent almost three years as an Intermodal Equipment Operator at BNSF Railway, at the
Tukwila intermodal facility. The operational details here come from that, not from a
textbook. The concurrency and database work is the part I built to learn cloud data
engineering.

## The problem it models

A yard runs on knowing where every unit is. When the database lags behind what is
physically happening on the ground you get dry runs (a driver shows up for a box that
is not available), misparks, and capacity numbers nobody trusts. Two workers going after
the same container is not a rare edge case either, it happens constantly during a train
offload.

So the question this project answers is: if several workers are hitting the same table at
once, what actually keeps the data correct, and what does it cost.

## Run it

```bash
pip install -r requirements.txt
python simulate.py
```

No AWS account needed. That runs a full shift against an in-memory stand-in
(`mock_dynamo.py`) and prints the correctness checks, the write contention count, and the
read cost.

The engines are not modified to make this work. `main.py`, `crane.py`, `hostler.py`,
`outgate.py` and `dispatch_check.py` run exactly as they would against real DynamoDB.
`config.get_table()` just hands them a different table object. Every conditional write and
every query is the real one. Only the simulated drive and lift times are compressed, with
`--speed`.

```bash
python simulate.py --unsafe               # same shift with the conditional writes removed
python simulate.py --claim dispatch       # claim before driving instead of racing
python simulate.py --cranes 2 --hostlers 4
python simulate.py --verbose              # show the engines' own output
python test_yard.py                       # 52 tests
```

## How a unit moves

Two directions, and they are different jobs.

**Import.** Comes in on a train, leaves on a truck.

```
Trackside_Hold  ->  Buffer_Hold      ->  Parked  ->  Departed (Road)
(on the railcar)    (crane set it         (hostler     (customer cleared
                     on a chassis)         parked it)   the gate and took it)
```

If a hostler already has a chassis backed in when the crane picks the box up, it goes to
`Rendezvous_Wait` instead of `Buffer_Hold` and that hostler takes it straight off.

**Export.** Comes in on a truck, leaves on a train.

```
Ingate_Hold  ->  Parked  ->  Awaiting_Rail  ->  Loaded_Rail  ->  Departed (Rail)
(driver         (hostler     (hostler took      (crane set      (train left)
 dropped it)     parked it)   it trackside)      it in a well)
```

`Current_Status` stays `Departed` for both. Which way it left is a separate field,
`Departure_Mode`. Splitting the terminal state in two would have broken eleven readers
that filter on `Departed`, so it is one state with an attribute.

**Stack order is enforced.** A double stack well holds a bottom and a top. The top comes
off first on offloads, and the bottom goes in first on loading. Each unit carries
`Railcar_ID`, `Well_Position` and `Blocked_By`, and the crane will not lift something that
is still pinned under another box. A unit that is claimed but not yet lifted still blocks,
because it is physically still sitting there.

## What keeps it correct

Every state change is a conditional write. The hostler updating a unit to `Parked`
requires that it is still `Ingate_Hold` when the write lands. If another hostler got there
during the drive, the write is rejected and this one goes back and rescans. No locks and
no coordinator process.

To show that this is actually doing something rather than just sounding good, there is a
control mode. `--unsafe` runs the identical shift with the conditions removed:

| | Conditional writes on | Conditional writes off |
| --- | :---: | :---: |
| Containers double parked | 0 | many |
| Park writes for N containers | exactly N | more than N |
| Conflicts raised | one per lost race | 0, nothing detects them |

The unsafe run raises zero database errors and quietly corrupts the yard. That is the
point. The conflict count in a normal run is not overhead, it is the number of misparks
the database stopped.

**Three ways to pick the next unit**, and they trade off differently:

- `head` takes the front of the queue, so every worker goes after the same box and all but
  one lose. Losers rescan and can lose again, so contention grows with crew size, not just
  with volume. Measured: 60 containers with 2 hostlers gives about 2.5 lost races per
  container, 200 containers with 6 hostlers gives about 8.
- `random` draws from anywhere in the queue. Collisions mostly go away, and so does
  arrival order.
- `dispatch` claims the unit before driving to it. A lost race costs a retry instead of a
  wasted trip, and arrival order is preserved. This is what a real dispatcher does.

## Reads

The engines query a `Current_Status` GSI (`StatusIndex`) through `config.query_status()`,
so a hostler looking for gate units reads only the gate units.

This was not the original design. It used to poll with `Scan` plus a filter, which reads
every row in the table and charges for every row no matter how few match. The cost of that
grows with the square of yard size, because the number of polls grows with the yard too:

| Containers | Rows read | Per container |
| ---: | ---: | ---: |
| 100 | 61,250 | 612x |
| 200 | 244,900 | 1,224x |
| 400 | 940,200 | 2,350x |

Four times the rows for twice the containers, measured. Loading a real day of PANYNJ
volume is what made that visible. At demo scale it looks fine.

## Demand forecasting

`demand_forecast.py` works on real monthly TEU volume for nine US container ports (BTS
dataset `iahn-a7j4`, 2020 to 2023), including NY/NJ.

44 months for one port is not enough to train on, so the model pools all nine ports and
learns the shape rather than the level. That comparison is the result:

| Model | MAPE | vs naive |
| :--- | ---: | ---: |
| naive seasonal (same month last year) | 22.3% | baseline |
| ridge, one port only | 11.7% | +48% |
| ridge, pooled across ports | 8.9% | +60% |
| gradient boosting, pooled | 6.2% | +72% |

Pooling beats the single port model by 24% on identical folds, same algorithm. Evaluation
is rolling origin, one step ahead, never shuffled.

Unsupervised passes on the same data: PCA finds one national demand factor explaining 60%
of variance across the nine ports, peaking May 2022 and bottoming May 2020. KMeans on
seasonal shape separates the West Coast ports from the East and Gulf group without being
told where any of them are.

Classical seasonal decomposition is also in here as a comparison, and it is a useful one.
On the stable 2000 to 2015 PANYNJ series it holds 4.1% MAPE against a held out six months,
beating a naive seasonal baseline at 8.6%. On the 2020 to 2023 COVID window it goes to
36.4% and loses to the naive baseline, because a straight line trend cannot represent the
import surge and the destocking crash after it. Newer data is not automatically better
data.

## What is in the repo

| File | What it does |
| :--- | :--- |
| `main.py` | Arrivals, spot assignment, SPOT# reservation |
| `crane.py` | Unloading and loading, stack precedence, sweep strategy |
| `hostler.py` | Yard moves both directions, dual cycling |
| `outgate.py` | Road departures for imports |
| `train.py` | Outbound train, well capacity, cutoff, departure |
| `dispatch_check.py` | Gate authorization check |
| `config.py` | Settings, GSI query helper, scan helper |
| `mock_dynamo.py` | In-memory DynamoDB stand-in, so this runs with no account |
| `simulate.py` | Runs a shift, collects telemetry, checks invariants |
| `test_yard.py` | 52 tests |
| `build_manifest.py` | Builds a day's manifest from real port volume |
| `demand_forecast.py` | Supervised and unsupervised models on BTS port data |
| `contention_analysis.py` | Does queue depth or yard occupancy drive contention |
| `benchmark_sweep.py` | Crane sweep strategy comparison on a simulated clock |
| `app.py` | Streamlit dashboard |

## What this does not model

Worth being straight about the edges.

- Chassis are unlimited. At Tukwila we usually had hundreds of bare chassis staged, so
  they were not the constraint, but a terminal that runs short of them behaves very
  differently.
- Containers are one per spot. No multi tier stacking, so no dig penalty when the box
  somebody wants is buried under two others.
- Ground crew removing cone locks is a fixed delay, not a crew with limited capacity.
- No hazmat segregation, no reefer plug in tracking, no bad order or M&R track.
- Weight distribution and destination blocking on the outbound train are not enforced.

One known gap in the harness itself. The exactly once check miscounts when the yard is
seeded with starting inventory, because the expected write count is derived per container
arrival and does not account for units already on the ground. A seeded run reports failure
when nothing is actually wrong. Replacing it with a per container duplicate transition
check fixes it, and stops the count going stale every time the state machine gains a state.

One thing that is not a gap, just slow: `contention_analysis.py` needs 15 to 20 minutes for
a full pass. Three claim strategies, each running 200 containers against 800 seeded units
at speed 0.05. It is not stalled. The seeded inventory and the slow clock are both load
bearing, so shortening the run costs you the result.

## Next

1. Multi tier stacking with the dig penalty, and stacking by predicted dwell so the
   forecasting work actually feeds an operational decision.
2. Outbound train constraints that bind. Well capacity is currently sized to fit
   everything, so nothing ever misses a cutoff, and missing the cutoff is the pressure
   that makes export interesting.
3. An append only move log on DynamoDB Streams. `Parked_By_Employee` is stamped on the
   container record, so a later move overwrites the earlier one and only the most recent
   operator is on file. A damage claim needs the whole chain.

### Note on single table design

Adding a second record type (`SPOT#` reservations) to the table silently broke every
reader that assumed one shape: the dashboard counted spot locks as containers, the harness
raised a KeyError on records with no status, and scan pagination reset when a cursor row
was deleted mid scan. Nothing errored. The numbers were just wrong. If you put more than
one entity type in a table, every read has to filter by type, and the failure mode when you
forget is silence.
