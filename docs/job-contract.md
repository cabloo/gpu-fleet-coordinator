# The job contract

A job is any program the coordinator can start with a command line. It does not import anything
from this repository. It has to do five things, and [`demo/job/train.py`](../demo/job/train.py) does
all of them with the standard library.

Several names below are fixed by the code and cannot be changed from the config: the flags
`--out` and `--init-from`, the checkpoint file `ckpt_latest.pt`, and the patterns of files that are
brought home.

## 1. Describe itself in its config

The config you queue carries a reserved `job` section. Nothing else tells the coordinator how to run
the job, so a new kind of job needs no change to the coordinator.

```json
{
  "seed": 0,
  "steps": 12,

  "job": {
    "manifest_version": 1,
    "run": ["python", "train.py"],
    "completion_artifact": "results.json",
    "resume": {"flag": "--init-from"},
    "setup": {"pip": ["numpy>=2,<3"], "apt": []},
    "resources": {"est_minutes": 30, "cores": 1, "vram_gb": 0.6, "requires_gpu": true}
  }
}
```

| field | meaning |
|---|---|
| `run` | the command prefix, relative to the root of the shipped tree. It must not contain `--config`: the queue client appends the path you named. |
| `completion_artifact` | a path under the job's output directory. The task is `done` only once this file has been pulled home. |
| `resume.flag` | declares that the job can resume. The worker always passes `--init-from`, whatever is written here. |
| `resume.none` | instead of `flag`: a written reason why this job cannot resume. |
| `setup.pip`, `setup.apt` | packages installed on the box before the first run. |
| `resources.est_minutes` | the expected run time. Placement, the rent decision and the budget all read it. |
| `resources.cores`, `resources.vram_gb` | the footprint of one lane of this job. Declare both or neither; one alone is ignored. |
| `resources.slots` | lanes the task occupies (default 1). A job running K worker processes claims K. |
| `resources.requires_gpu` | `true` restricts the job to boxes registered with a GPU, and charges it the default 0.6 GB when `vram_gb` is absent. Omitted or `false`, the job may land on a box without a card and is charged no GPU memory. |
| `resources.box` | run only on this box: an instance id or an owned box's label. |
| `resources.force_box` | with `box`, on an owned box: skip the admission gates. |

A job estimated at more than 10 minutes must declare `resume`, either with a flag or with a reason
under `none`. The queue client refuses it otherwise, because a lost box would restart it from zero.

The `job` section is not part of the run's identity (below): changing an estimate or a core count
never makes a run look new.

## 2. Answer `--print-run-identity`

Before queueing, the client runs the job once in the project's own tree:

```
python train.py --config path/to/config.json [your args] --print-run-identity
```

The job must print one JSON object as the last line of standard output and exit 0, before touching a
GPU or the file system:

```json
{"config": {"seed": 0, "steps": 12, "run": {"out_dir": ""}}}
```

`config` is everything that decides what the run computes. The client hashes it twice:

- with the seed: two tasks with the same hash are the same run, and the second is refused;
- without the seed: two tasks with the same hash are the same arm of a comparison, and the client
  says so.

These paths are left out of both hashes because they cannot change the result: `run.out_dir`,
`run.name`, `run.tag`, `run.device`, `logging.out_dir`, `logging.tb_dir`, `checkpoint` and `job`.
The seed is read from `seed` or `run.seed`.

A non-zero exit here is how a job rejects bad arguments before anything is queued.

## 3. Write to `--out`, and know what comes home

The worker starts the job in the root of the shipped tree, with that tree's `src/` on `PYTHONPATH`,
and appends `--out <directory>`.

**Only these files are brought home from that directory:**

| when | what |
|---|---|
| while the job runs | `ckpt_latest.pt` (and its spare `ckpt_latest.pt.prev`), and everything under `tb/` |
| when it finishes | the completion artifact, `*.json`, `*.jsonl`, `*.log`, `ckpt_*.pt`, and everything under `tb/`, at any depth |

Everything else is deleted with the box's copy of the task. A `model.pt`, a `metrics.csv` or a
`plot.png` is lost. Name what you want kept to match one of the patterns.

## 4. Checkpoint as it goes, and resume

Write the checkpoint as `ckpt_latest.pt` at the top of `--out`, atomically (write a temporary file,
then rename). The coordinator pulls it home every 5 minutes. When a box is lost, drained or
preempted, the task is requeued and started on another box with

```
... --init-from <path to the carried checkpoint>
```

A job that accepts the flag and ignores it restarts from zero each time, which is the failure the
resume rule exists to prevent. A job that writes its checkpoint under another name is never
checkpoint-pulled and so never resumes.

Checkpoints are also how the coordinator knows a job is alive. A running task whose checkpoint and
TensorBoard events have both been silent for 90 minutes (`stall_timeout_min`) is stopped as stalled
and retried.

## 5. Accept `--set` if you want sweeps

A sweep varies a config by appending `--set dotted.path=value` arguments. A job that does not parse
`--set` would run every cell with the base config. The client guards against that with a text check
of a `.py` entry script: it refuses to build the sweep when it finds no `--set` handling there. It
cannot judge a module started with `-m`, and `--force` overrides it.

## What the job does not have to do

- It does not talk to the coordinator. There is no client library and no callback.
- It does not need credentials for anything; the box has none.
- It does not handle preemption. The worker stops it after its next write of `ckpt_latest.pt`.
- It does not need to know which box it is on, or that it shares a GPU. It does need to fit in
  the footprint it declared.
