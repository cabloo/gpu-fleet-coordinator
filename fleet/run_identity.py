"""Canonical config hashing for run identity (docs/specs/run-registry.spec.md).

Stdlib only — importable by any entrypoint without pulling in torch/tyro/etc. Two tasks are the
same exact run iff `config_hash` matches (seed included); the same A/B arm iff `arm_hash`
matches (seed excluded too).
"""

from __future__ import annotations

import copy
import hashlib
import json

# Non-semantic fields: output locations, display names/tags, logging sinks, device placement,
# checkpoint cadence. A path is excluded iff changing it cannot change the science of the run.
EXCLUDE_PATHS: frozenset[str] = frozenset({
    "run.out_dir",
    "run.name",
    "run.tag",
    "run.device",
    "logging.out_dir",
    "logging.tb_dir",
    "checkpoint",          # harness-owned section: enabled/every_s/grace_min/reason (operational)
    "job",                 # COORDINATOR-owned section: run argv, completion artifact, resume flag,
    #                        setup deps, resource hints (job-artifact-contract spec). Same rationale
    #                        as `checkpoint` — re-sizing est_minutes or cores changes how a run is
    #                        SCHEDULED, never what it computes, so it must not fork run identity or
    #                        every capacity tweak would defeat the duplicate guard.
})

SEED_PATHS: frozenset[str] = frozenset({"seed", "run.seed"})


def _strip_path(cfg: dict, path: str) -> None:
    parts = path.split(".")
    node = cfg
    for key in parts[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _prune_empty(node: dict) -> None:
    for key in list(node.keys()):
        v = node[key]
        if isinstance(v, dict):
            _prune_empty(v)
            if not v:
                del node[key]


def canonical_config(cfg: dict, exclude: frozenset[str]) -> str:
    """Deep-strip every dotted path in `exclude`, drop dicts left empty, serialize
    deterministically (sorted keys, no whitespace) so key order never affects the result."""
    stripped = copy.deepcopy(cfg)
    for path in exclude:
        _strip_path(stripped, path)
    _prune_empty(stripped)
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"))


def config_hash(cfg: dict) -> str:
    """First 16 hex chars of sha256 of the canonical JSON (seed INCLUDED)."""
    canon = canonical_config(cfg, EXCLUDE_PATHS)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def arm_hash(cfg: dict) -> str:
    """Same as `config_hash` but with `SEED_PATHS` also stripped — two tasks are the same
    A/B arm iff this matches."""
    canon = canonical_config(cfg, EXCLUDE_PATHS | SEED_PATHS)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def print_run_identity(cfg: dict, *, seed, out_dir, tag=None, device=None) -> None:
    """The `--print-run-identity` handshake every dispatchable entrypoint implements (registry
    spec invariant 9): print `{"config": ...}` to stdout and let the caller `return`/exit 0
    before any GPU/filesystem work. `seed` stays top-level (matches `SEED_PATHS`); `out_dir`/
    `tag`/`device` are nested under `"run"` (matches `EXCLUDE_PATHS`) regardless of whether the
    caller's own `cfg` already carries flat `out`/`tag`/`device`/`seed` keys (some entrypoints'
    trainer-loop `run()` takes them separately, some fold them into `cfg`) — pass whichever
    values are actually in effect for this invocation and this strips/rebuilds either way."""
    payload = {k: v for k, v in cfg.items() if k not in ("seed", "out", "out_dir", "tag", "device")}
    payload["seed"] = seed
    run = {"out_dir": out_dir}
    if tag is not None:
        run["tag"] = tag
    if device is not None:
        run["device"] = device
    payload["run"] = run
    print(json.dumps({"config": payload}))
