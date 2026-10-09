"""Host-side enforcer of a box's time-of-day HARD caps (capacity spec, box-pause inv. 20/20a/20c).

Reads the SAME schedule the coordinator infers slots from and applies the current window's caps:
  * CPU  — `docker update --cpus <cores·cpu>` on the worker container: a kernel cgroup limit training
           cannot exceed no matter how many threads a task spawns.
  * GPU  — `nvidia-smi -pl <watts>` per card when the window declares `gpu_power` (a fraction of the
           card's DEFAULT power limit, clamped to its [min, max]); a window without it restores the
           default. VRAM itself cannot be capped — the coordinator bounds it by packing (`vram`).

Run on the box's Docker HOST as root (it needs docker + nvidia-smi). `owned_box_setup.sh` installs
it as a 1-minute systemd timer against the schedule the COORDINATOR PUSHES into the worker's bind
mount (inv. 20b), with `--uncapped-if-missing`:

    python3 /opt/fleet-worker/box_capacity_apply.py --uncapped-if-missing \\
        --config /var/lib/fleet-worker/control/capacity.json --container fleet-worker

Older hosts run it from cron against a repo checkout's `configs/capacity/<label>.json` instead.
Every action is a no-op when the cap already holds, so a tight timer is cheap. The schedule file is
DATA from inside the container (inv. 20d): it is validated by `capacity.load` and every cap is clamped
to the hardware, so the worst a bad file can do is lift the box's own throttle."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capacity  # noqa: E402


def power_target_w(frac: float | None, default_w: float, min_w: float, max_w: float) -> int:
    """Watts to set for a `gpu_power` fraction (None = the card's default), clamped to the card."""
    want = default_w if frac is None else frac * default_w
    return int(round(min(max(want, min_w), max_w)))


def parse_power_query(stdout: str) -> list[dict]:
    """Rows of `nvidia-smi --query-gpu=index,power.limit,power.default_limit,power.min_limit,
    power.max_limit --format=csv,noheader,nounits`. A card whose limits read `[N/A]` / `Not Supported`
    (common on laptop GPUs) comes back with `settable=False` rather than being dropped, so the caller
    can say why it skipped it."""
    rows = []
    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        try:
            cur, dflt, lo, hi = (float(p) for p in parts[1:])
            rows.append({"index": idx, "limit": cur, "default": dflt, "min": lo, "max": hi,
                         "settable": True})
        except ValueError:
            rows.append({"index": idx, "settable": False, "raw": line.strip()})
    return rows


def _run(cmd: list[str], dry: bool) -> int:
    print(("[dry-run] " if dry else "") + " ".join(cmd))
    if dry:
        return 0
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"box_capacity_apply: {cmd[0]} failed rc={r.returncode}: "
              f"{(r.stderr or r.stdout).strip()}", file=sys.stderr)
    return r.returncode


def apply_cpu(cpus: float | None, container: str, dry: bool) -> int:
    """`cpus=None` lifts the cap, by setting it to EVERY core this host has. Clamped to this host's
    cores; skipped when the container already carries exactly this cap.

    ⛔ NOT `--cpus 0` (box-pause 20b-1). `docker update` reads a zero as "field not supplied" and
    leaves the existing limit in place, exiting 0 — so the old uncap was a silent no-op that re-ran
    every minute against a container still held at its last window's cap (`desktop`, 2026-10-03:
    10 of 20 cores, eleven minutes after its schedule was removed)."""
    host_cores = float(os.cpu_count() or 1)
    want = host_cores if cpus is None else round(min(float(cpus), host_cores), 2)
    r = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", container],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"box_capacity_apply: no container {container!r}: {r.stderr.strip()}", file=sys.stderr)
        return r.returncode
    try:
        if int(r.stdout.strip() or 0) == int(want * 1e9):
            return 0
    except ValueError:
        pass
    return _run(["docker", "update", "--cpus", str(want), container], dry)


def apply_gpu_power(frac: float | None, dry: bool) -> int:
    """Set every card's power limit for `frac` (None = default). No GPU / no nvidia-smi = no-op."""
    if shutil.which("nvidia-smi") is None:
        return 0
    q = subprocess.run(["nvidia-smi", "--query-gpu=index,power.limit,power.default_limit,"
                        "power.min_limit,power.max_limit", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    if q.returncode != 0:
        print(f"box_capacity_apply: nvidia-smi query failed: {q.stderr.strip()}", file=sys.stderr)
        return q.returncode
    rc = 0
    for g in parse_power_query(q.stdout):
        if not g["settable"]:
            if frac is not None:
                print(f"box_capacity_apply: GPU {g['index']} has no settable power limit "
                      f"({g['raw']}) — gpu_power skipped")
            continue
        want = power_target_w(frac, g["default"], g["min"], g["max"])
        if abs(g["limit"] - want) < 1.0:
            continue
        rc |= _run(["nvidia-smi", "-i", str(g["index"]), "-pl", str(want)], dry)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--container", required=True)
    ap.add_argument("--uncapped-if-missing", action="store_true",
                    help="a missing config means the coordinator holds NO schedule for this box: "
                         "remove the CPU cap and restore the default GPU power limit")
    ap.add_argument("--no-gpu", action="store_true", help="never touch GPU power limits")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    path = Path(a.config).expanduser()
    if not path.exists() and a.uncapped_if_missing:
        cpus, gpu_frac = None, None
    else:
        try:
            sched = capacity.load(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"box_capacity_apply: bad schedule {path}: {e} — caps left unchanged",
                  file=sys.stderr)
            return 2
        now = datetime.datetime.now(datetime.timezone.utc)
        if capacity.active_window(sched, now) is None:
            print("box_capacity_apply: no active window — leaving caps unchanged")
            return 0
        cpus, gpu_frac = capacity.cpu_cores_cap(sched, now), capacity.gpu_power_fraction(sched, now)
    rc = apply_cpu(cpus, a.container, a.dry_run)
    if not a.no_gpu:
        rc |= apply_gpu_power(gpu_frac, a.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
