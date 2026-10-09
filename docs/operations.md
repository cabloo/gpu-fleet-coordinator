# Operating it

Everything below is run from a checkout of this repository unless it says otherwise. The data root
is `experiments/` beside the code: the registry (`runs.sqlite`), code snapshots and shipping blobs
(`.dispatcher/`), and each task's results (`<group>/<name>/`).

## Before the first job

Two defaults are those of the project this came from, and both stop a newcomer:

- **A `vastai` executable must be on the path**, even if you rent nothing: every dispatcher pass
  starts by asking it for the account's instances. Install the real tool, or copy
  `demo/bin/vastai` (a stub that exits non-zero) onto the path for a fleet of owned machines only.
- **Compiled shipping is on.** It needs Cython and an interpreter matching the box image's Python,
  and it compiles only the directories in `bundle_compile_packages`, which default to two
  directories of the original project. Either switch it off, or name your own packages:

  ```
  sqlite3 experiments/runs.sqlite \
    "INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile', 'false')"
  ```

  With it on and no toolchain, `runq add` exits 5 and queues nothing.

## Start the dispatcher

```
python fleet/dispatcher.py            # the poll loop; it holds a lock, one per data root
python fleet/dispatcher.py --once     # a single pass
```

On first start it writes its default settings into the registry. With a `vastai` that has no
account behind it and no box registered, it holds every task and records why.

`--dry-run` is not a safe preview: it skips shipping, renting and idle teardown, but it still
reconciles against Vast.ai, claims tasks, logs holds, and can destroy a rental stuck in
provisioning. Do not point it at a live registry.

## Queue work

```
python fleet/runq.py add --config path/to/config.json --group G --name N --by me [-- job args...]
python fleet/runq.py ls [--group G] [--state running] [--json]
python fleet/runq.py show <task-id>
python fleet/runq.py cancel <task-id> --reason "why"      # works on a running task too
python fleet/runq.py dupes --of <task-id>
python fleet/runq.py rate
python fleet/runq.py spend
```

`--config` names the job's config in full. The tree that is shipped is the git repository
containing that file, as it is on disk (uncommitted changes included, ignored files excluded), or
the config's own directory when it is not in a repository. See [the job contract](job-contract.md).

Exit codes: 0 done, 1 a check failed (`colocate --verify`, `box probe`), 2 invalid, 3 duplicate
refused, 4 illegal state change, 5 build failed, 6 API unreachable, 7 API refused.

Useful flags on `add`:

| flag | effect |
|---|---|
| `--by NAME` | who queued it. Required unless `RUNQ_ACTOR` is set or the git branch name will do; `main` and `master` will not. |
| `--est-minutes M` | overrides the config's estimate. |
| `--priority P` | default 50. |
| `--probe` | priority 90, for a short run whose answer blocks a decision. Refused above 30 minutes. |
| `--force` | queue despite an identical earlier run. |
| `--max-retries N` | default 10. |
| `--box LABEL` | run only on this box. |
| `--colocate KEY` | run on the same box as every other task with this key. |
| `--init-from PATH` | start from an existing checkpoint. |

**Names are used once.** A task's group and name are unique across every row, failed and cancelled
ones included. The duplicate check hashes configuration and arguments, not code. So:

- after a code fix, queue under a new `--name`, and add `--force` if the earlier run is still open
  or finished `done`;
- a failed or cancelled task is not a duplicate, but its name is taken: requeue it under a new one.

A tree that is already packed can be queued without running the job locally (the shipping artifact
is still built locally):

```
python fleet/runq.py submit CODE.tar.gz --config path/inside/the/tar.json --group G --name N --by me
```

## Sweeps

One file expands into ordinary tasks:

```json
{
  "sweep_version": 1,
  "config": "configs/demo.json",
  "group": "demo",
  "axes": {"seed": [0, 1, 2], "plan_kappa": [0.5, 2.0]},
  "cells": [{"plan_kappa": 4.0}]
}
```

```
python fleet/runq.py sweep demo.sweep.json --by me --dry-run     # print the cells, queue nothing
python fleet/runq.py sweep demo.sweep.json --by me
```

`axes` is a product over `--set` paths; `cells` adds explicit extras. Expansions above 64 cells are
refused unless `--max-cells` raises the limit. A sweep cell has 3 retries unless the file sets
`max_retries`.

Running a sweep again skips the cells that are already queued, running or done, so a sweep whose
queueing was interrupted can simply be re-run. It does not requeue a cell that failed or was
cancelled: that cell's name is taken, and the re-run stops there. Queue such cells under a new group
or name.

By default the cells of one seed are kept on one box, so arms can be compared as pairs. Check that
this held before reporting a comparison, and switch it off for a grid that is not a comparison:

```
python fleet/runq.py colocate --group demo --verify      # exit 1 if a seed's arms were split
python fleet/runq.py sweep grid.sweep.json --by me --no-colocate
```

## Watch a campaign

```
python fleet/watch.py --group G                 # one line per event worth acting on
python fleet/watch.py --group G --once          # a status snapshot
```

Lines are tagged: `FAIL` (with the exception from the job's log), `INFRA` (with the retry budget
left), `STALL`, `QUEUED-COLD` (with the reason a task is being held), `REQUEUE`, `PREEMPT`, `BOX`,
`COLOCATE`, `JOINED`, `READOUT`, `DONE`, `CANCELLED`, `END`. The watcher is read-only: it reads the
registry and the result directories that have been pulled home. It exits when every task has
settled, or after 24 hours (`--max-hours`).

Other read-only reports:

```
python fleet/calibration.py [--group G] [--trend]    # expected against actual: run time, cost, packing
python fleet/fleet_util.py --once                    # under-used capacity, with the fix it proposes
python fleet/est_defaults.py --dry-run               # run-time estimates learned from finished tasks
python fleet/prune_experiments.py                    # what retention would delete (add --apply)
```

## Add a machine you own

A box is a Linux machine the coordinator reaches as root over ssh, normally a Docker container of
the PyTorch image fixed in `fleet/dispatcher.py` (`BOX_IMAGE`). `fleet/owned_box_setup.sh` builds
one on a fresh Ubuntu install with an NVIDIA card: driver, Docker, the worker image with an ssh
daemon on port 2222, and a timer that applies the box's resource caps.

```
fleet/owned_box_setup.sh --bundle > setup-owned-box.sh        # a self-contained copy
scp setup-owned-box.sh you@newbox:
ssh -t you@newbox sudo bash setup-owned-box.sh --label newbox --fleet-key "ssh-ed25519 AAAA... coordinator"
```

`--fleet-key` is the public half of the key the coordinator uses for ssh; it is the only key let
into the worker. Then, on the coordinator:

```
python fleet/register_owned_box.py --label newbox --host newbox.lan --port 2222 --slots 8 \
    --gpu-name "RTX 4070"
```

Registration installs and starts the worker. Give `--gpu-name` for a box with a card: a job that
declares `requires_gpu` is placed only on boxes registered with one. An owned box costs nothing in
the placement arithmetic, so it never loses to a machine that would cost extra, and it is never
torn down. If it becomes unreachable its tasks are requeued and it is used again when it returns.

To share a machine with its owner, give it a schedule at `configs/capacity/<label>.json`:

```json
{
  "tz": "America/New_York",
  "cores": 20, "vram_gb": 8,
  "windows": [
    {"from": "23:00", "to": "07:00", "cpu": 0.875, "vram": 0.875},
    {"from": "07:00", "to": "23:00", "cpu": 0.5, "vram": 0.75, "gpu_power": 0.6}
  ]
}
```

Each window gives the fraction of the machine the fleet may use. The coordinator infers the lanes
from it and pushes it to the box, where the timer enforces the CPU share and the GPU power limit.
A box without a card needs no schedule.

```
make pause BOX=newbox      # freeze its work in place; back soon
make hold BOX=newbox       # no new work; running tasks finish
make drain BOX=newbox      # checkpoint its work so it requeues elsewhere
make resume BOX=newbox
```

## Rent on Vast.ai

Install the `vastai` tool, give it your API key, and add your ssh public key to your Vast.ai
account. The dispatcher calls `vastai` for offers, rentals and teardown, and labels every instance
it creates `runq_<task id>`. It adopts an instance it finds in the account only when that label
names a task in its own registry.

A task that cannot be packed onto a live box is weighed against renting: the budget must allow it,
and when the fleet already has machines the queued backlog must be large enough to be worth a box
(invariant 4 of [the dispatcher spec](specs/task-dispatcher.spec.md)). That backlog test does not
apply when the fleet has no machine at all, or to a task above priority 50, so with an empty fleet
and a funded account a single queued task rents at once. The settings that bound spending:

| setting | default | meaning |
|---|---|---|
| `max_hourly_usd` | 1.50 | committed dollars per hour across all rented boxes |
| `max_instance_dph` | 0.08 | price ceiling for one offer, dollars per hour |
| `balance_floor_usd` | 3.00 | no new rentals once the account balance is below this |
| `idle_timeout_min` | 10 | an idle rental is torn down after this long |
| `hard_cap_hours` | 48 | a rental never lives longer than this |
| `warm_idle_max`, `max_warm_free_slots` | 2, 10 | how much idle rented capacity may be kept for queued work |

Set `max_hourly_usd` to 0 to run on owned machines only.

## Settings

Settings are rows of the registry's `settings` table: a key and a JSON value. There is no command
for them yet; write the row with `sqlite3` as shown above.

When a setting takes effect depends on who reads it:

- the dispatcher reads most settings once, at start: restart it after a change;
- `bundle_compile` and its companions are read by `runq` each time it queues;
- `launch_gate`, the worker-roll limits, the two data-root marks and per-box rows are read on
  every pass.

**Eight settings are reset at start if they hold a default that was later replaced**, because the
dispatcher cannot tell an old default from your choice of the same value. Writing the left-hand
value and restarting gives you the right-hand one:

| setting | written | after restart |
|---|---|---|
| `max_hourly_usd` | 1.0 | 1.5 |
| `max_instance_dph` | 0.4 | 0.08 |
| `max_slots_cap` | 8 | 11 |
| `provision_timeout_min` | 45 | 15 |
| `heartbeat_stale_min` | 5 | 15 |
| `claim_timeout_min` | 15 | 30 |
| `bundle_compile_cache_max` | 24 | 64 |
| `consolidate_enabled` | true | false |

To hold one of those values, write one beside it (a budget of 0.99 instead of 1.0). The list is
`_SETTING_MIGRATIONS` in `fleet/dispatcher.py`.

The defaults, with the measurement behind each, are `DEFAULT_SETTINGS` at the top of
`fleet/dispatcher.py`. The ones most often changed:

| setting | default | meaning |
|---|---|---|
| `vram_per_lane_gb`, `cores_per_lane` | 0.6, 1 | the default lane, for jobs that declare no footprint |
| `max_slots_cap` | 11 | most lanes on one rented box |
| `bundle_compile` | true | ship Cython-compiled modules of the directories in `bundle_compile_packages` |
| `bundle_sign` | false | sign each bundle (`DISPATCHER_BUNDLE_SIGN_KEY`, `DISPATCHER_BUNDLE_PUBKEY`) |
| `preempt_enabled` | false | let a task at least 30 priority points higher evict a running one at its next checkpoint; also required for draining a rental and for shrinking a box's capacity under running work |
| `consolidate_enabled` | false after the first restart | drain a rental whose whole load fits on capacity that stays alive; needs `preempt_enabled` |
| `checkpoint_pull_every_min` | 5 | how often a running task's checkpoint is pulled home |
| `stall_timeout_min` | 90 | silence after which a running task is stopped as stalled |
| `poll_seconds` | 30 | pause between dispatcher passes |
| `data_root_hold_free_gb`, `data_root_resume_free_gb` | 5, 10 | the dispatcher holds while less than the first is free on the data root's filesystem and resumes at the second; 0 for the first turns the hold off |

## Environment

| variable | used by | meaning |
|---|---|---|
| `RUNQ_ACTOR` | client | default for `--by` |
| `FLEET_DATA_ROOT` | everything | the data root, instead of `experiments/` beside the code |
| `FLEET_SITE_DIR` | dispatcher, client | a directory holding `capacity/<label>.json`, `est_defaults.json` and `machines.deny`, instead of the copies inside this checkout |
| `DISPATCHER_SSH_KEY` | dispatcher | private key for reaching boxes, instead of the default identity |
| `DISPATCHER_NTFY_TOPIC`, `DISPATCHER_NTFY_SERVER` | dispatcher | push alerts through ntfy; unset means none |
| `DISPATCHER_BUNDLE_SIGN_KEY`, `DISPATCHER_BUNDLE_PUBKEY` | dispatcher | bundle signing |
| `RUNQ_TRANSPORT`, `COORD_API_URL`, `COORD_API_CA`, `COORD_API_CERT`, `COORD_API_KEY` | client | queue through the HTTPS API instead of writing the registry |

## Using it from another project

By default the coordinator assumes it lives inside the project whose runs it stores: it finds the
data root through the repository around its own files and reads its site data from its own tree. A
project that pins this repository at a commit instead tells it where things are:

```
export FLEET_DATA_ROOT=/path/to/project/experiments      # registry, snapshots, results
export FLEET_SITE_DIR=/path/to/project/fleet-site        # capacity/, est_defaults.json, machines.deny
python /path/to/gpu-fleet-coordinator/fleet/runq.py add --config configs/job.json --group G --name N --by me
```

Both are read by every tool, so the dispatcher and the client must be started with the same values.
The tree that is shipped is still the repository containing the config you name, not this one.
`fleet/owned_box_setup.sh --name NAME` sets what a host's image, container, directories and timer
are called (default `fleet`); always re-run a host under the name it was set up with.

## Queueing from another machine

`fleet/coordinator/` holds an HTTP API for the registry (`api_server.py`), an nginx front that
requires a client certificate from a private authority (`api-proxy/nginx.conf`, `api_pki.sh`), and
the entry point and health checks of a container for them (`fleet/Dockerfile.coordinator`). Each
certificate carries a list of the actions it may take, and nginx limits its request rate.

With `RUNQ_TRANSPORT=api` the client builds the shipping artifact locally and sends it. The
submission writes nothing to the data root, and there is no fallback: if the API cannot be reached
the command fails. The client does still open the registry at `--db` for the duplicate check and
the build settings, so it needs the coordinator's data root readable, for example mounted
read-only. The compose file and host setup of the fleet this came from are not included; you wire
the three pieces together yourself.

## When something goes wrong

- `python fleet/runq.py show <task-id>` prints the task's events, including each hold with its
  reason.
- After a failure or a cancel, the task's `run.log` is in `experiments/<group>/<name>/`. A
  `crash.json` is there only if the job wrote one to `--out`.
- `task_failed` is not retried. It is usually the job's own failure, and also the outcome of a
  rejected bundle, a failed package install on the box, or an unreadable resume checkpoint.
  `infra_failed` is retried from the last checkpoint; each requeue uses half a retry of the task's
  budget. A preemption or a drain uses none.
- A box that cannot receive bundles is quarantined so one bad box does not absorb the queue.
- A `data_root_low` event, an `[ALERT]` line and one push mean the data root has less than
  `data_root_hold_free_gb` free, or that a write to it has just failed for lack of space (the
  event's `why` says which). The dispatcher is holding: it places nothing, ships nothing and
  pulls no results, checkpoints or TensorBoard files. Running jobs keep running, cancels still
  work, and a job that finishes waits on its box, where its files are kept for 12 hours. Free
  space (`python fleet/prune_experiments.py --apply`) and it resumes by itself at
  `data_root_resume_free_gb`, logging `data_root_ok`. The free space it reads is in every
  `poll_cycle` event, under `data_root.free_gb`.
- Do not run tests in a shell that has the API variables set unless `tests/conftest.py` is in
  place: it is what stops a test from queueing into a live fleet.
