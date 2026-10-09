# gpu-fleet-coordinator

A job queue for people who run **many small, checkpointed experiments** and own a few GPUs.
It packs several jobs onto each card, fills your own machines first, rents cheap
[Vast.ai](https://vast.ai) boxes only when it has to, and carries checkpoints between machines so
an interrupted job resumes somewhere else.

It was written for one research project and has run that project's experiments every day since
July 2026. This repository is that code, published as it is.

```
you ──runq add──▶ registry (SQLite) ◀──poll── dispatcher ──ssh/rsync──▶ worker on each box
                                                 │                          │
                                                 └── rents / tears down     └── runs jobs side by side,
                                                     Vast.ai boxes              writes checkpoints
```

## What it is good at

- **Packing many jobs onto one GPU.** A box is divided into lanes sized by memory and cores (by
  default 0.6 GB of GPU memory and one core each), not into whole GPUs. The worker on the box then
  holds each launch until measured load and free memory allow it, and the coordinator learns each
  job group's real footprint from what it measures.
- **Spending as little as possible.** One budget covers the whole fleet (dollars per hour, plus a
  balance floor). A job goes to the machine where it adds the fewest dollars, so a machine you own
  never loses to one that would cost extra. Rented boxes are torn down after 10 idle minutes.
- **Surviving a lost machine.** The coordinator pulls each job's checkpoint home every 5 minutes
  while it runs. When a box disappears, the job is requeued and resumes on another box from that
  checkpoint. No shared disk or bucket is involved.
- **Keeping rented machines out of your secrets.** Work is pushed to a box and results are pulled
  back; a box never holds a cloud key or a way to reach home. Each job arrives as a bundle whose
  manifest binds every file by SHA-256, optionally signed, and code can be shipped compiled.
- **Not paying for the same run twice.** Before anything is queued the job prints its own
  configuration, which is hashed. An identical run is refused. A sweep is one file that expands into
  jobs.
- **Fair comparisons.** A sweep keeps the arms of one seed on one machine, because results here
  were bit-identical within a machine and differed slightly across machines.
- **Telling you why.** Every decision is a row in an event log: why a job is held, why a box was
  rented, why a task was requeued. A report compares estimated run time and cost with what happened.

## What it is not good at

- **Any cloud but one.** Renting means Vast.ai, through its command-line tool, called directly from
  the dispatcher. There is no provider interface, and the dispatcher will not start without a
  `vastai` executable on the path even when you rent nothing. Other machines can join only as boxes
  you own and can reach over ssh.
- **Rentals that just work.** Of 922 boxes it has rented, 573 never completed a job. The specs
  record the causes that were found: boxes that never start, ssh proxies that hang, hosts too slow
  to receive a bundle. Three quarters of all completed jobs ran on the four owned machines. The
  owned-machine path is the reliable one.
- **Big jobs.** One job is one process tree on one box. There are no multi-node jobs, no
  services or endpoints, no interactive environments, no volumes or object storage.
- **Teams.** There is one operator and one budget. The `--by` label says who queued a job; it is
  not an account. There are no quotas, and no permissions per person beyond the list of actions each
  client certificate may take on the optional API.
- **Jobs that do not follow its contract.** A job must answer `--print-run-identity`, write to
  `--out`, and leave a completion file. Only some files come home: JSON, logs, `ckpt_*.pt` and
  TensorBoard events; anything else is deleted with the box copy. To resume, a job must take
  `--init-from` and write `ckpt_latest.pt`; both names are fixed. A job that writes no checkpoint
  and no TensorBoard event for 90 minutes is stopped as stalled. See
  [the job contract](docs/job-contract.md).
- **Moving running work.** Preemption by priority, and draining a rental back onto owned machines
  once they free up, are both built and both switched off by default. The registry records 119
  such drains from when they were on.
- **Being read quickly.** `fleet/dispatcher.py` is one file of more than 8,000 lines. Its comments
  record the incident behind most rules, which helps when changing it and not when skimming it.
- **Being configured.** Settings live in a table of the registry. There is no command to change
  one; you write the row, and eight of them are reset at start if they hold an old default. Compiled
  shipping is on by default, needs a build toolchain, and by default compiles two directories that
  exist only in the original project. Data lives in `experiments/` beside the code unless two
  environment variables say otherwise.
- **Running anywhere.** Boxes are Linux machines reached as root over ssh, normally Docker
  containers of one fixed PyTorch image. The setup script for an owned box targets Ubuntu with an
  NVIDIA card. Nothing here has been run by anyone but its author.

One more edge: the duplicate check hashes the configuration and arguments, not the code, and a
task's group and name can never be reused. Re-running after a code fix therefore needs a new name
and `--force`.

## Try it

One machine, no GPU, no ssh daemon, no account:

```
git clone https://github.com/cabloo/gpu-fleet-coordinator
cd gpu-fleet-coordinator
demo/local_demo.sh
```

The demo starts the real dispatcher and the real worker. A stand-in `ssh` (`demo/bin/ssh`) runs
every "remote" command in a local directory, so a box is just a folder. It queues three seeds of
[`demo/job`](demo/job), refuses a fourth that repeats the first, and waits about 20 seconds:

```
== wait for the three tasks
   claimed queued shipped
   running running running
   done done done
== what happened to seed0
   2026-10-08T23:41:34Z  add        queued by demo
   2026-10-08T23:41:34Z  claim      pack on instance -1: $0.0000 marginal, fit 13.75<=5255999.9912469 (free slots 3)
   2026-10-08T23:41:34Z  ship       shipped to instance -1
   2026-10-08T23:41:36Z  start      worker reported start
   2026-10-08T23:41:50Z  done       completion artifact verified
== its result, pulled home from the box
{"seed": 0, "steps": 12, "total": 78.0}
```

The demo cannot rent anything: its `vastai` refuses every call and its budget is $0.

## How a job moves

1. **`runq add --config path/to/config.json`** runs the job once with `--print-run-identity`,
   hashes the configuration it prints, and refuses a duplicate. It then snapshots the project's
   working tree (committed or not), builds the shipping artifact, and writes a task row.
2. **The dispatcher** polls the registry. For each queued task it picks the live box where the task
   costs least, or rents one if the budget allows and waiting is not the better choice, or holds
   the task and records why.
3. **Shipping** pushes one bundle to the box over rsync. The code itself is sent once per box and
   referenced by hash afterwards.
4. **The worker** on the box claims the task, verifies the bundle, waits for its launch gate, and
   runs the job in its own directory.
5. **While it runs** the dispatcher pulls the checkpoint and TensorBoard events home, measures the
   box, and watches for a stall, a dead worker or a lost box. An infrastructure failure requeues
   the task with its checkpoint and uses half a retry (a task has 10 by default, a sweep cell 3). A
   failure of the job itself is not retried.
6. **Done** means the job exited and its completion file was pulled home and found. Results land in
   `experiments/<group>/<name>/`.

## Using it for real

- **Write a job**: [docs/job-contract.md](docs/job-contract.md). [`demo/job/train.py`](demo/job/train.py)
  is a complete one in about 70 lines.
- **Queue, sweep, watch, cancel; add an owned machine; rent on Vast.ai; change a setting**:
  [docs/operations.md](docs/operations.md).
- **Why each rule exists**: [docs/specs](docs/specs). Each feature has a spec with numbered
  invariants, and comments in the code cite them.

Requirements: Python 3.11 or later; `ssh` and `rsync` on the coordinator; and a `vastai` executable
on the path (the real tool if you rent; a stub that exits non-zero, like `demo/bin/vastai`, if you
do not). The coordinator, the worker and the queue client use only the standard library.
`cryptography` is needed only for signed bundles, and capacity schedules need a time-zone database
(the system's, or the `tzdata` package). Before your first `runq add`, either set the
`bundle_compile` setting to `false` or point `bundle_compile_packages` at your own packages and
install the build toolchain; [operations](docs/operations.md#settings) explains both.

## Track record

From the registry of the fleet this was written for, on 2026-10-08, three months after its first
task:

| | |
|---|---|
| tasks queued | 11,243 |
| completed | 8,794 |
| cancelled | 1,718 |
| failed in the job itself | 711 |
| gave up after infrastructure retries | 12 |
| requeued after an infrastructure failure | 573 |
| ran on more than one machine before finishing | 481 |
| stopped as stalled | 233 |
| rental boxes drained back onto owned machines | 119 |
| machines | 4 owned, 922 rented over the period |
| completed on owned machines / on rentals | 6,689 / 2,105 |
| rented boxes that never completed a task | 573 |

These are one fleet's numbers, not a benchmark.

## Compared with SkyPilot and dstack

Both are far more capable general tools: many clouds, Kubernetes, multi-node jobs, services, teams.
Use one of them unless the list under "good at" is exactly your problem. Reading their documentation
in October 2026, the differences that matter here were:

| | this | [SkyPilot](https://docs.skypilot.ai/en/stable/examples/managed-jobs.html) | [dstack](https://dstack.ai/docs/concepts/fleets/) |
|---|---|---|---|
| smallest unit placed | a lane, a fraction of a GPU | a cluster per managed job (pools reuse workers) | an instance, or an even block of one |
| checkpoints across machines | pulled home by the coordinator | written by the job to a bucket or volume | not described; a retry resubmits the run |
| budget | one cap for the whole fleet | none found | a maximum price per run |
| moving running work to cheaper machines | built, off by default | none found | none found |
| where it can run | Vast.ai and your own machines | many clouds, Kubernetes, Slurm, ssh pools | many clouds, Kubernetes, ssh fleets |

"None found" means not on the pages read, not confirmed absent.

## Layout

```
fleet/                  everything that runs: dispatcher.py, runq.py (the queue client),
                        spool_worker.py (on the box), registry_db.py, bundle.py, ...
fleet/coordinator/      the optional HTTPS API in front of the registry: server, nginx front,
                        certificate tooling, container entry point
demo/                   the single-machine demo and an example job
docs/                   the job contract, operations, and the specs
tests/                  more than 1,400 tests; the wire protocol is also tested against a real sshd in Docker
```

## Tests

```
pip install pytest cryptography
python -m pytest -q -m "not slow"      # two to three minutes
python -m pytest -q -m slow            # real subprocess lifecycles
```

Run them from a git checkout. `tests/conftest.py` forces the queue client's local transport for
every test, so a test can never queue into a live coordinator. Nine tests of the watcher's result
digest need `torch` and `tensorboard`, and the capacity-schedule tests need a time-zone database
(`pip install tzdata` where the system has none). `tests/test_docker_integration.py` drives the real
ship, claim, run, pull, preempt and resume path against an sshd container and is skipped where
Docker is absent.

## Where this came from

The coordinator was extracted from a private research repository in October 2026. Machine names,
addresses, rented-machine ids and the project's name were replaced; the code is otherwise what
runs the live fleet. Comments and specs keep their dates and incident history, and some refer to
experiments, trainers or documents of that project that are not here. The site's own deployment
(its compose file, host setup and roll scripts) and the specs that describe its network are not
included.

## License

MIT. See [LICENSE](LICENSE).
