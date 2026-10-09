"""Sweep-file parsing + deterministic expansion (docs/specs/runq-sweep.spec.md).

Stdlib-only and side-effect-free: `runq sweep` feeds the expansion into the ordinary manifest-add
path; the tests golden-pin `expand()`/`cell_args()` directly. A cell is a dict of dotted `--set`
path -> JSON value; the harness parses each override back with identical JSON semantics, so an
encoding can never alias two different configs to different hashes.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re

SWEEP_VERSION = 1
ALLOWED_KEYS = {"sweep_version", "config", "group", "axes", "cells", "name_template",
                "est_minutes", "priority", "slots", "max_retries", "colocate"}

#: Dotted-path LAST SEGMENTS that identify the seed of a cell, for automatic pairing (Behavior 9).
#: `seeds` (a one-element list) is this repo's dominant convention — 1366 uses against 16 of `seed`
#: — so both are recognised, case-insensitively.
SEED_SEGMENTS = ("seed", "seeds")


class SweepError(Exception):
    """A validation failure in the sweep file / expansion -> runq exits 2, nothing enqueued."""


def parse_sweep(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise SweepError("sweep file: top level must be an object")
    for key in raw:
        if key not in ALLOWED_KEYS:
            raise SweepError(f"sweep file: unknown key {key!r}")
    if raw.get("sweep_version") != SWEEP_VERSION:
        raise SweepError(f"sweep file: sweep_version must be {SWEEP_VERSION}")
    if not isinstance(raw.get("config"), str) or not raw["config"]:
        raise SweepError("sweep file: 'config' must be a non-empty path to the base trainer "
                         "config FILE (v2; it replaced 'job', which named a directory holding a "
                         "job.json). Naming the config here is what stops a cell inheriting a "
                         "config the sweep never mentions.")
    axes = raw.get("axes")
    if not isinstance(axes, dict):
        raise SweepError("sweep file: 'axes' must be an object of dotted-path -> array")
    for path, values in axes.items():
        if not isinstance(values, list) or not values:
            raise SweepError(f"sweep file: axes[{path!r}] must be a non-empty array")
    cells = raw.get("cells", [])
    if not isinstance(cells, list) or any(not isinstance(c, dict) or not c for c in cells):
        raise SweepError("sweep file: 'cells' must be an array of non-empty objects")
    if not axes and not cells:
        raise SweepError("sweep file: expansion is empty (no axes, no cells)")
    for field, typ in (("group", str), ("name_template", str), ("est_minutes", int),
                       ("priority", int), ("slots", int), ("max_retries", int)):
        if field in raw and not isinstance(raw[field], typ):
            raise SweepError(f"sweep file: {field!r} must be a {typ.__name__}")
    colo = raw.get("colocate")
    if colo is not None and not (isinstance(colo, bool) or isinstance(colo, str)
                                 or (isinstance(colo, list)
                                     and colo and all(isinstance(p, str) and p for p in colo))):
        raise SweepError("sweep file: 'colocate' must be false (disable), a dotted axis path, or a "
                         "non-empty array of dotted axis paths naming what makes two cells the SAME "
                         "PAIRED SEED. Omit it for the default (auto-detect a seed axis).")
    return raw


def cell_args(overrides: dict) -> list[str]:
    """The cell's entry_args: `--set path=value` in sorted-path order, values as compact JSON —
    byte-stable so identity hashes never depend on axis declaration order."""
    args: list[str] = []
    for path in sorted(overrides):
        args += ["--set", f"{path}={json.dumps(overrides[path], separators=(',', ':'), sort_keys=True)}"]
    return args


def _render(value) -> str:
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, (dict, list)):
        canon = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return hashlib.sha1(canon.encode()).hexdigest()[:6]
    return str(value)


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


def _short_key(path: str) -> str:
    segments = [s for s in path.split(".") if not s.isdigit()]
    return segments[-1] if segments else path


def _auto_name(overrides: dict, ambiguous: set[str]) -> str:
    terms = []
    for path in sorted(overrides):
        key = path.replace(".", "-") if path in ambiguous else _short_key(path)
        terms.append(_sanitize(key) + _sanitize(_render(overrides[path])))
    return "_".join(terms)


# A cell name becomes a DIRECTORY COMPONENT (`experiments/<group>/<name>/`), and every common Linux
# filesystem caps one component at 255 BYTES. `_auto_name` concatenates every overridden key AND its
# value, so a cell with many small genes overruns it silently: the run itself proceeds (the box spools
# by task UUID) while nothing can be written or ingested home-side, and the first symptom is the
# WATCHER dying with `OSError: [Errno 36] File name too long` — a crash in the one process whose job is
# to notice failures. Measured 2026-07-29: three `edge_*` genes on a 9-gene cell produced a 262-char
# name. Caught here, at expansion, where the fix (`name_template`) is one line.
NAME_MAX_BYTES = 255


def _check_name_length(name: str) -> None:
    n = len(name.encode("utf-8"))
    if n > NAME_MAX_BYTES:
        raise SweepError(
            f"generated cell name is {n} bytes, over the {NAME_MAX_BYTES}-byte filesystem limit for a "
            f"directory component — artifacts could not be written or ingested, and the watcher would "
            f'die with "File name too long".\n  Set "name_template" in the sweep (e.g. "{{arm}}") to '
            f"name cells explicitly.\n  offending name: {name!r}")


def _template_name(template: str, overrides: dict) -> str:
    def sub(m: re.Match) -> str:
        path = m.group(1)
        if path not in overrides:
            raise SweepError(f"name_template: {{{path}}} is not an axis/cell path of this cell")
        return _render(overrides[path])
    return _sanitize(re.sub(r"\{([^{}]+)\}", sub, template))


def _key_render(value) -> str:
    """`_render`, except that a ONE-ELEMENT list reads as its element.

    Only for colocation keys, never for cell NAMES (which are golden-pinned). The repo writes the
    seed as `"seeds": [1]`, and `_render` hashes any list — so without this every group key would be
    a 6-char digest, unreadable in a `hold` line and in `runq colocate` at exactly the moment
    somebody is trying to work out which seed did not pair."""
    if isinstance(value, list) and len(value) == 1:
        return _render(value[0])
    return _render(value)


def pairing_paths(cells: list[tuple[str, dict]], by=None) -> list[str]:
    """The dotted paths whose values make two cells the SAME PAIRED SEED (Behavior 9).

    Explicit `by` wins and is validated against the expansion — a path no cell overrides would
    silently collapse every cell into one group, which is the failure mode most likely to be
    mistaken for the feature working. Otherwise: every path whose last non-numeric segment is
    `seed`/`seeds`."""
    if by:
        paths = sorted({by} if isinstance(by, str) else set(by))
        present = {p for _n, ov in cells for p in ov}
        missing = [p for p in paths if p not in present]
        if missing:
            raise SweepError(
                f"colocate: {missing} is not an axis/cell path of this expansion, so every cell "
                f"would fall into ONE colocation group and the whole sweep would be forced onto a "
                f"single box. Name a path the cells actually override, or pass --no-colocate.")
        return paths
    return sorted({p for _n, ov in cells if isinstance(ov, dict) for p in ov
                   if _short_key(p).lower() in SEED_SEGMENTS})


def colocate_keys(group: str, cells: list[tuple[str, dict]], by=None) -> dict[str, str]:
    """`{cell name: colocation group key}` — which cells must land on ONE box together.

    THE RULE, and why it is the default rather than a flag (owner, 2026-08-16: *"have paired seed
    tests always use it"*). Arms `1..n` of a seed are a PAIRED measurement and are only readable if
    they ran on the same machine; seeds are not paired with each other and must stay free to spread
    across the fleet. So: one group per distinct seed value, and **no** group spanning two seeds.

    A sweep with NO seed path is a single-seed sweep — its seed comes from the base config, so every
    cell IS the same paired seed and the whole expansion is one group. That is the 1-seed scout, the
    most common paired comparison in this repo, and the case that most needs pairing.
    """
    paths = pairing_paths(cells, by)
    out: dict[str, str] = {}
    for name, overrides in cells:
        if not paths:
            out[name] = f"{group}:seed-from-config"
            continue
        terms = []
        for p in paths:
            short = _sanitize(_short_key(p))
            # A cell that does not override the seed path takes the base config's seed, which is one
            # specific seed — so it groups with the other such cells, not with any overridden value.
            terms.append(f"{short}{_sanitize(_key_render(overrides[p]))}" if p in overrides
                         else f"{short}-from-config")
        out[name] = f"{group}:" + "_".join(terms)
    return out


def expand(sweep: dict) -> list[tuple[str, dict]]:
    """The deterministic cell list: cartesian product over sorted axes (values in file order),
    then explicit `cells` verbatim. Returns [(name, overrides), ...]; duplicate names raise."""
    axes = sweep["axes"]
    paths = sorted(axes)
    product = ([dict(zip(paths, combo)) for combo in itertools.product(*(axes[p] for p in paths))]
               if paths else [])
    all_cells = product + [dict(c) for c in sweep.get("cells", [])]

    distinct_paths = {p for cell in all_cells for p in cell}
    by_short: dict[str, set[str]] = {}
    for p in distinct_paths:
        by_short.setdefault(_short_key(p), set()).add(p)
    ambiguous = {p for group in by_short.values() if len(group) > 1 for p in group}
    template = sweep.get("name_template")
    named = [((_template_name(template, c) if template else _auto_name(c, ambiguous)), c)
             for c in all_cells]
    seen: dict[str, dict] = {}
    for name, c in named:
        if name in seen and seen[name] != c:
            raise SweepError(f"generated name collision: {name!r} — use name_template")
        if name in seen:
            raise SweepError(f"duplicate cell: {name!r} appears twice in the expansion")
        _check_name_length(name)
        seen[name] = c
    return named
