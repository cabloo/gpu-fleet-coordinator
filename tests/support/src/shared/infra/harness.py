"""Stand-in for the trainer harness the toy trainers in these tests were written against.

The real harness (typed config, checkpoint cadence, resume, logging) belongs to the project this
coordinator was extracted from and is not part of this repository. The queue client asks a job
exactly ONE thing before it spends anything -- `--print-run-identity` -- so this implements that
handshake and the flags around it with the standard library, and nothing else:

    --config PATH   --set dotted.path=value   --out DIR   --init-from PATH   --print-run-identity

It is test scaffolding. `fleet/smoke_entrypoint.py` is the reference for a real job.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

JOB_SECTION = "job"          # coordinator-reserved section of a config; never part of run identity


@dataclasses.dataclass
class RunSection:
    out_dir: str = ""
    tag: str = ""
    device: str | None = None


@dataclasses.dataclass
class CheckpointSection:
    enabled: bool = True
    every_s: float = 300.0
    grace_min: float = 15.0
    reason: str = ""


class _Run:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir

    def save_latest(self, payload, force: bool = False) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        with open(os.path.join(self.out_dir, "results.json"), "w") as f:
            json.dump(payload, f)


def _parse(argv: list[str]) -> dict:
    cli = {"config": None, "sets": [], "out": None, "init_from": None, "print_identity": False}
    takes_value = {"--config": "config", "--out": "out", "--init-from": "init_from"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--print-run-identity":
            cli["print_identity"] = True
            i += 1
        elif a == "--set" and i + 1 < len(argv):
            cli["sets"].append(argv[i + 1])
            i += 2
        elif a in takes_value and i + 1 < len(argv):
            cli[takes_value[a]] = argv[i + 1]
            i += 2
        else:
            raise SystemExit(f"config error: unknown or incomplete flag {a!r}")
    return cli


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: _merge(base[k], v) if k in base else v for k, v in over.items()}}
    return over


def _apply_set(raw: dict, expr: str) -> None:
    dotted, eq, rawval = expr.partition("=")
    keys = dotted.split(".")
    if not eq or not all(keys):
        raise SystemExit(f"config error: --set {expr!r}: expected dotted.path=value")
    try:
        value = json.loads(rawval)
    except json.JSONDecodeError:
        value = rawval
    node = raw
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            raise SystemExit(f"config error: --set {expr!r}: {k} is not an object")
    if keys[-1] not in node:
        raise SystemExit(f"config error: --set {expr!r}: no such field {dotted}")
    node[keys[-1]] = value


def run_trainer(config_cls: type, run_fn) -> int:
    cli = _parse(sys.argv[1:])
    raw = {}
    if cli["config"] is not None:
        with open(cli["config"]) as f:
            raw = json.load(f)
    raw.pop(JOB_SECTION, None)
    cfg = _merge(dataclasses.asdict(config_cls()), raw)
    for expr in cli["sets"]:
        _apply_set(cfg, expr)
    if cli["out"]:
        cfg["run"]["out_dir"] = cli["out"]
    if cli["print_identity"]:
        run = cfg.get("run") or {}
        cfg["run"] = {k: v for k, v in {"out_dir": run.get("out_dir", ""), "tag": run.get("tag") or None,
                                        "device": run.get("device")}.items() if v is not None}
        print(json.dumps({"config": cfg}))
        return 0
    if not cfg["run"]["out_dir"]:
        print("config error: no output dir (--out or run.out_dir)", file=sys.stderr)
        return 2
    run_fn(_Run(cfg["run"]["out_dir"]))
    return 0
