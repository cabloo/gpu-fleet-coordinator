"""Synthetic entrypoint for exercising the dispatcher/registry pipeline end-to-end without a
real trainer (docs/specs/task-dispatcher.spec.md, `entrypoints.py`'s only `live` entry).

Implements the full contract dispatchable entrypoints are expected to have: `--print-run-identity`
(registry spec), `--out`, `--init-from` (resume — dispatcher spec invariant 16). Deliberately
stdlib-only (no torch) so it's cheap to run in unit tests / the docker integration test / a real
$0.25 paid smoke.

    python fleet/smoke_entrypoint.py --out out/ --updates 10 --ckpt-every 2
    python fleet/smoke_entrypoint.py --print-run-identity --updates 10   # handshake only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def build_config(a: argparse.Namespace) -> dict:
    return {
        "seed": a.seed,
        "updates": a.updates,
        "sleep_per_step": a.sleep_per_step,
        "ckpt_every": a.ckpt_every,
        "fail_at": a.fail_at,
        "run": {"tag": a.tag, "out_dir": a.out},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--print-run-identity", action="store_true")
    p.add_argument("--out", default="out")
    p.add_argument("--tag", default="smoke")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--updates", type=int, default=10)
    p.add_argument("--sleep-per-step", type=float, default=0.2)
    p.add_argument("--ckpt-every", type=int, default=2)
    p.add_argument("--init-from", default=None)
    p.add_argument("--fail-at", type=int, default=None)
    a = p.parse_args(argv)

    if a.print_run_identity:
        print(json.dumps({"config": build_config(a)}))
        return 0

    os.makedirs(a.out, exist_ok=True)
    ckpt_path = os.path.join(a.out, "ckpt_latest.pt")

    start_update = 0
    if a.init_from and os.path.exists(a.init_from):
        with open(a.init_from) as f:
            start_update = json.load(f).get("update", 0)
        print(f"[smoke] resumed from {a.init_from} at update {start_update}", flush=True)

    for update in range(start_update + 1, a.updates + 1):
        time.sleep(a.sleep_per_step)
        if a.fail_at is not None and update >= a.fail_at:
            print(f"[smoke] simulated failure at update {update}", file=sys.stderr, flush=True)
            return 1
        if update % a.ckpt_every == 0 or update == a.updates:
            tmp = ckpt_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"update": update, "seed": a.seed}, f)
            os.replace(tmp, ckpt_path)
            print(f"[smoke] checkpoint at update {update}", flush=True)

    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump({"updates_done": a.updates, "seed": a.seed, "tag": a.tag}, f)
    print("[smoke] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
