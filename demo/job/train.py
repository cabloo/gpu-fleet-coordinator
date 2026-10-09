"""A complete job for this coordinator, standard library only.

What the coordinator asks of a job (docs/job-contract.md):

    --print-run-identity   print {"config": ...} on the last line of stdout and exit 0, before any
                           work: this is how a run is hashed and a duplicate refused before spending
    --config PATH          the config that names this job (the queue client appends it)
    --set key=value        an override on top of the config (what a sweep varies)
    --out DIR              where results and checkpoints go
    --init-from PATH       resume from a checkpoint the coordinator carried to this box

and what the job leaves behind:

    ckpt_latest.pt         written atomically as it goes; pulled home while the job runs
    results.json           the completion artifact: a task is only `done` once this exists
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--print-run-identity", action="store_true")
    p.add_argument("--config", required=True)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--out", default="")
    p.add_argument("--init-from", default=None)
    a = p.parse_args()

    with open(a.config) as f:
        cfg = json.load(f)
    cfg.pop("job", None)                 # the coordinator's section: scheduling, never identity
    for expr in a.set:
        key, _, raw = expr.partition("=")
        if key not in cfg:
            print(f"unknown config field {key!r}", file=sys.stderr)
            return 2
        cfg[key] = json.loads(raw)
    if a.print_run_identity:
        print(json.dumps({"config": {**cfg, "run": {"out_dir": a.out}}}))
        return 0

    os.makedirs(a.out, exist_ok=True)
    ckpt = os.path.join(a.out, "ckpt_latest.pt")
    step, total = 0, 0.0
    if a.init_from and os.path.exists(a.init_from):
        with open(a.init_from) as f:
            state = json.load(f)
        step, total = state["step"], state["total"]
        print(f"resumed at step {step}", flush=True)
    while step < cfg["steps"]:
        time.sleep(cfg["sleep_per_step"])
        step += 1
        total += (cfg["seed"] + 1) * step
        if step % cfg["ckpt_every"] == 0:
            with open(ckpt + ".tmp", "w") as f:
                json.dump({"step": step, "total": total}, f)
            os.replace(ckpt + ".tmp", ckpt)
            print(f"step {step}/{cfg['steps']} checkpointed", flush=True)
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump({"seed": cfg["seed"], "steps": step, "total": total}, f)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
