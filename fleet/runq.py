"""`runq` CLI — the run registry's only writer besides the dispatcher (docs/specs/run-registry.spec.md).

    python fleet/runq.py add --group demo --name t1 --entrypoint smoke --est-minutes 5 \\
        -- --updates 10
    python fleet/runq.py ls
    python fleet/runq.py show <task-id>
    python fleet/runq.py cancel <task-id>
    python fleet/runq.py rate
    python fleet/runq.py spend

Exit codes (invariant, Output contract): 0 success · 2 validation error · 3 duplicate refused ·
4 illegal state transition · 5 BUILD FAILED (ship-artifact-build spec inv. 13 — the queuer compiles,
so a compile error or a broken toolchain blows the whole task and queues NOTHING).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling imports below, even when
# this module is loaded via importlib (tests) rather than run directly as a script

import hashlib  # noqa: E402
import registry_db  # noqa: E402  (fleet/ isn't a package; loaded via sys.path)
import entrypoints  # noqa: E402
import est_defaults  # noqa: E402  (learned est_minutes fallback; docs/specs/est-defaults.spec.md)
import code_snapshot  # noqa: E402  (ship the working tree, not a git ref; docs/specs/code-snapshot.spec.md)
import job_manifest  # noqa: E402  (self-describing job contract; docs/specs/job-artifact-contract.spec.md)
import sweep_expand  # noqa: E402  (declarative sweeps; docs/specs/runq-sweep.spec.md)
import bundle  # noqa: E402  (compile/overlay; the QUEUER builds now — ship-artifact-build.spec.md)
import api_client  # noqa: E402
import artifact_store  # noqa: E402  (ship-ready blob store; ship-artifact-build.spec.md)
from run_identity import arm_hash, config_hash  # noqa: E402

# The shared queue every worktree converges on by default (registry_db.shared_experiments_root's
# docstring) -- NOT worktree-relative, unlike ROOT above (which stays worktree-scoped since the
# identity handshake/shipped payload must reflect THIS worktree's own code).
DEFAULT_DB = str(registry_db.shared_experiments_root() / "runs.sqlite")
HANDSHAKE_TIMEOUT_S = 60

# Scheduling priority (run-registry spec). DEFAULT_PRIORITY mirrors `dispatcher.DEFAULT_PRIORITY`.
# PROBE_PRIORITY must exceed DEFAULT_PRIORITY by more than the dispatcher's `preempt_priority_margin`
# (30) or a --probe task could never displace ordinary work — 90 > 50 + 30. It is also > 50, which
# additionally exempts it from invariant 4d's backlog bar, so a probe may rent immediately rather
# than wait for a queue to build.
DEFAULT_PRIORITY = 50
PROBE_PRIORITY = 90
PROBE_MAX_MINUTES = 30

# Cells in one colocation group past which `runq sweep` warns. Not a limit — a group legitimately
# larger than any box's lane count still runs, it just serialises, and the operator should be told
# that BEFORE the spend rather than discover it as a slow queue. Sized against the fleet's real
# boxes (owned boxes cap at 12-16 usable lanes; a rental is typically 6-8).
COLOCATE_WARN_CELLS = 8


def _resolve_argv(argv: list[str]) -> list[str]:
    return [sys.executable if a == "python" else a for a in argv]


def _split_on_dashdash(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _run_identity_handshake(entry: entrypoints.Entrypoint, entry_args: list[str],
                            root: Path = ROOT) -> dict:
    """Run `<entry.argv> <args> --print-run-identity` in `root` (its own `src/` on PYTHONPATH) to
    hash the config before any spend. `root` defaults to this worktree (named entrypoints); the
    `add <dir>` path passes the job directory so the manifest's own code answers the handshake."""
    cmd = _resolve_argv(entry.argv) + entry_args + ["--print-run-identity"]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    try:
        out = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                              timeout=HANDSHAKE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise SystemExit(f"runq add: identity handshake failed to run: {e}")
    if out.returncode != 0:
        raise SystemExit(
            f"runq add: entrypoint rejected args (exit {out.returncode}): {out.stderr.strip()}")
    try:
        parsed = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
        return parsed["config"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise SystemExit(f"runq add: unparseable --print-run-identity output: {e}")


def _git_sha(root: Path = ROOT) -> str:
    """Best-effort HEAD sha for provenance only (job-artifact-contract inv. 8). Empty string when
    `root` is not a git checkout — no add ever fails for lack of git, and nothing reads it for
    correctness (ship uses the content-addressed snapshot, code-snapshot spec inv. 6/7)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _git_branch(root: Path = ROOT) -> str:
    """Best-effort current branch of `root`, for actor auto-derivation (run-registry inv. 15).
    Empty string on failure or a detached checkout (where `--abbrev-ref` prints the literal
    'HEAD') — an empty result simply means --by/$RUNQ_ACTOR must supply the identity instead."""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    b = out.stdout.strip() if out.returncode == 0 else ""
    return "" if b == "HEAD" else b


# run-registry invariant 15: a task's actor (`created_by`) must be a usable identity, never a
# non-identifying default. The reserved set below is rejected case-insensitively.
_RESERVED_ACTORS = frozenset({"unknown", "master", "main", "head"})
_ACTOR_RE = re.compile(r"[A-Za-z0-9][\w./+-]{0,63}\Z")


def _is_meaningful_actor(label: str) -> bool:
    return bool(_ACTOR_RE.match(label)) and label.lower() not in _RESERVED_ACTORS


def _resolve_actor(a: argparse.Namespace) -> tuple[str | None, int | None]:
    """Resolve the required `created_by` actor (run-registry inv. 15). First non-empty of
    --by / $RUNQ_ACTOR / current git branch, then validated. Returns (label, None) on success or
    (None, 2) after printing a fix-it message — no $USER fallback, no silent 'unknown'."""
    env_actor = os.environ.get("RUNQ_ACTOR")
    raw = a.by or env_actor or _git_branch()
    label = (raw or "").strip()
    if _is_meaningful_actor(label):
        return label, None
    src = "--by" if a.by else "$RUNQ_ACTOR" if env_actor else "git branch"
    example = _git_branch()
    example = example if _is_meaningful_actor(example) else "my-agent-or-branch"
    print(f"runq {a.cmd}: --by is required — a branch or agent name for attribution. "
          f"Resolved {label!r} from {src}, which is not a usable identity "
          f"(empty, malformed, or one of master/main/detached-HEAD/unknown). "
          f"Pass e.g. --by {example}", file=sys.stderr)
    return None, 2


_LABEL_INTERPRETERS = ("python", "python3", "bash", "sh", "env", "srun", "torchrun")


def _manifest_label(run: list[str]) -> str:
    """A readable, non-null value for the `entrypoint` column of a manifest task. A
    `python -m native.X` run → `native.X`; a script run → the script path.

    ⛔ THIS COLUMN IS **NOT** DISPLAY-ONLY, whatever its previous docstring said. `est_defaults.py`
    GROUPS BY IT to build the fleet's learned per-entrypoint `est_minutes` — the number `runq add`
    uses when `--est-minutes` is omitted, i.e. what the dispatcher packs and preempts against. The
    old fallback was `run[-1]`, the LAST argv token, which is an argument VALUE for every
    script-style run: 287 of 5620 rows were keyed on things like `32`, `256`, `0.05` and `.`
    (20 cells of `survival_vision_coord_convergence.py` all landed under `.`, its `--out` value).
    That both fragments a real script's samples across its argument values and injects junk
    entrypoints into the calibration table. Prefer the script/module; never an argument."""
    if not run:
        return "job"
    if len(run) >= 3 and run[0] in ("python", "python3") and run[1] == "-m":
        return run[2]
    for tok in run:                                    # the script being run, wherever it sits
        if tok.endswith(".py") or tok.endswith(".sh"):
            return tok
    for tok in run:                                    # not a script: first real token
        if tok not in _LABEL_INTERPRETERS and not tok.startswith("-"):
            return tok
    return run[0]


def _content_identity(code_hash: str, run: list[str], entry_args: list[str]) -> tuple[dict, str, str]:
    """Dedupe identity for a pre-built `submit`ted artifact whose code can't be run locally
    (job-artifact-contract inv. 12): content-address the (code_hash, run, args) triple instead of a
    `--print-run-identity` handshake. Returns (config, config_hash, arm_hash)."""
    payload = {"code_hash": code_hash, "run": list(run), "args": list(entry_args)}
    return payload, config_hash(payload), arm_hash(payload)


def _manifest_slots(a: argparse.Namespace, manifest: job_manifest.JobManifest) -> int:
    """Lanes this task occupies: explicit `--slots` wins, else the manifest's `resources.slots`,
    else 1.

    `resources.slots` was VALIDATED by the manifest parser and then never read — every manifest task
    took exactly one lane no matter what it declared. That is only safe while a task is a single
    process; once a job runs K worker processes (`workers` in the trainer config) a one-slot claim
    lets K-times-oversubscribed tasks pack onto the same box, because placement (`_fits_now`) checks
    slots and nothing else. A parallel job must therefore declare `slots == workers`, with
    `resources.cores` expressing the per-SLOT (i.e. per-worker) core share."""
    if a.slots is not None:
        return int(a.slots)
    declared = manifest.resources.get("slots")
    return max(1, int(declared)) if declared is not None else 1


EST_SANITY_FACTOR = 2.0


def _warn_if_est_contradicts_the_learned_default(label: str, est: int) -> None:
    """Warn when a manifest's declared `est_minutes` is wildly off the learned p90 for its entrypoint.

    ⛔ THE MANIFEST PATH NEVER CONSULTS THE LEARNED TABLE. `resolve_est_minutes` (the estimate
    feedback loop) is wired only into the NAMED-entrypoint path; a `--config` add takes
    `resources.est_minutes` verbatim and, if absent, hard-errors. Since config adds are how
    essentially all campaign work is queued, the calibration loop serves the one path almost nobody
    uses, and a hand-written estimate is never corrected by anything.

    Measured when this was added: declared/actual has a MEDIAN of 1.99x over 3920 completed tasks and
    50% are over-declared by >2x. Concretely, `m99_fullcur` declared 1150 against a measured 331 and
    the next campaign inherited it as 2200 — reserving 37h of fleet capacity for a ~10h job, which
    mis-prices every packing and preemption decision the task appears in.

    Deliberately a WARNING, not a correction: the declared value still wins. Overriding a config's
    own number at submission time would silently change what campaigns run under, and the estimate is
    load-bearing for `--probe` refusal and the resume contract."""
    try:
        learned = est_defaults.load_default(label)
    except Exception:                                   # never block an add on a sidecar problem
        return
    if not learned or est <= 0:
        return
    if est >= EST_SANITY_FACTOR * learned:
        print(f"runq add: WARNING est_minutes={est} is {est / learned:.1f}x the learned p90 "
              f"({learned}) for {label} — over-declaring reserves capacity the job never uses",
              file=sys.stderr)
    elif learned >= EST_SANITY_FACTOR * est:
        print(f"runq add: WARNING est_minutes={est} is {learned / est:.1f}x BELOW the learned p90 "
              f"({learned}) for {label} — under-declaring invites a stall-reap or a bad preempt",
              file=sys.stderr)


def _manifest_est_and_hint(a: argparse.Namespace, manifest: job_manifest.JobManifest
                           ) -> tuple[int | None, str | None, str | None]:
    """Resolve est_minutes + resource_hint for a manifest task: explicit CLI flags win, else the
    manifest's `resources` defaults (job-artifact-contract inv. 4/O5). Returns
    (est_minutes|None, resource_hint_json|None, error|None)."""
    res = manifest.resources
    est = a.est_minutes if a.est_minutes is not None else res.get("est_minutes")
    if est is None:
        return None, None, ("no est_minutes: pass --est-minutes or set resources.est_minutes "
                            "in the config's `job` section)")
    est = int(est)
    _warn_if_est_contradicts_the_learned_default(_manifest_label(manifest.run), est)
    if a.vram_per_lane_gb is not None:
        hint = {"vram_per_lane_gb": a.vram_per_lane_gb, "cores_per_lane": a.cores_per_lane}
    elif "vram_gb" in res and "cores" in res:
        hint = {"vram_per_lane_gb": float(res["vram_gb"]),
                "cores_per_lane": int(res["cores"])}
    else:
        hint = {}
    # PLACEMENT hints, forwarded verbatim from `resources` (job-artifact-contract inv. 4).
    # These are the axes the DISPATCHER reads that the per-lane footprint above does not express,
    # and until now nothing could produce them: `max_dph` (invariant 4f, lifts this task's own price
    # ceiling), `ram_per_lane_gb` (invariant 27, the RAM axis of `slots_for_offer`) and
    # `cpu_name_include` (invariant 4f, rent a NAMED CPU class) were all read by `dispatcher.py` and
    # documented as things "a task declares", but no code path ever wrote them into
    # `resource_hint_json` — so every one of them was dead. `res_defaults.resolve_hint` layers the
    # learned footprint per AXIS precisely so fields like these survive, which only matters once
    # they can exist. Unknown `resources` keys are already passed through by the manifest parser.
    for k in ("max_dph", "ram_per_lane_gb", "cpu_name_include", "box", "colocate",
              "requires_gpu", "force_box"):
        if k in res:
            hint[k] = res[k]
    # `--box` wins over a config-declared one: the flag is what an operator reaches for when they
    # already know which machine the question is about, and it should not be silently overridden by
    # a default baked into a config months ago. Same for `--colocate`.
    if getattr(a, "box", None):
        hint["box"] = str(a.box)
    if getattr(a, "colocate", None):
        hint["colocate"] = str(a.colocate)
    if getattr(a, "force_box", False):   # dispatcher inv. 4i; the flag can only turn it ON
        hint["force_box"] = True
    conflict = _hint_conflict(hint) or _force_box_error(a, hint)
    if conflict:
        return None, None, conflict
    return est, (json.dumps(hint) if hint else None), None


def _force_box_error(a: argparse.Namespace, hint: dict) -> str | None:
    """`--force-box` / `resources.force_box` on the FINAL hint (dispatcher inv. 4i-1). Returns the
    refusal, or None.

    Opens the registry ONLY when the hint actually carries the key, so an ordinary add is untouched.
    Under the API transport the structural half runs here and "is it a registered OWNED box" is the
    COORDINATOR's answer (422): its registry is the one of record, and a remote client may hold no
    copy of it — checking a local file there would refuse a legitimate request, or pass a wrong one."""
    if registry_db.FORCE_BOX_HINT_KEY not in hint:
        return None
    conn = None if api_client.enabled() else registry_db.connect(a.db)
    return registry_db.force_box_error(conn, hint)


def _hint_conflict(hint: dict) -> str | None:
    """`--box` and `--colocate` are MUTUALLY EXCLUSIVE (dispatcher inv. 4g). Returns the message, or
    None if the hint is coherent.

    They are opposite requests. `--box` says "I have already decided which machine, put it there";
    `--colocate` says "I have NOT decided, put it wherever you put its siblings". Honouring both is
    not a merge — either the operator's box wins (and the group silently splits when a sibling was
    pinned elsewhere) or the pin wins (and `--box` silently does nothing). Both are the class of
    failure this feature exists to remove, so the combination is refused at the point of spend
    rather than resolved by a precedence rule nobody will remember. A config's `resources.box`
    counts, which is why this is checked on the FINAL hint and not on the flags."""
    if hint.get("box") and hint.get("colocate"):
        return (f"--box and --colocate are mutually exclusive, and this task declares both "
                f"(box={hint['box']!r}, colocate={hint['colocate']!r}; a config's `resources.box` "
                f"counts). --box names the machine YOU chose; --colocate asks the COORDINATOR to "
                f"place this task wherever its siblings land. Drop one.")
    return None


def _dedupe_guard(conn, a: argparse.Namespace, chash: str, ahash: str) -> int | None:
    """Shared config-hash clash refusal + same-arm warning (registry spec inv. 3). Returns an exit
    code to return, or None to proceed."""
    clash = None if a.force else registry_db.find_clash(conn, chash)
    if clash:
        print(f"runq add: refused — task {clash['id']} ({clash['grp']}/{clash['name']}) "
              f"already has this exact config (state={clash['state']}); use --force to add "
              "anyway", file=sys.stderr)
        return 3
    arm_matches = registry_db.find_arm_matches(conn, ahash)
    if arm_matches and all(m["config_hash"] != chash for m in arm_matches):
        names = ", ".join(f"{m['id']} ({m['grp']}/{m['name']})" for m in arm_matches)
        print(f"runq add: warning — same arm as: {names} (different seed?)", file=sys.stderr)
    return None


def _blob_keep_max(conn) -> int:
    """Unreferenced ship blobs to retain. Shares `bundle_compile_cache_max` — one knob for "how much
    build output do we keep", since the blob store supersedes the compile cache it is sized like."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='bundle_compile_cache_max'").fetchone()
        return max(1, int(json.loads(row[0]))) if row else 64
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return 64



# ── the API transport (remote-submit spec M1) ──────────────────────────────────────────────────
#: Rows accumulated for ONE submission. `runq sweep` opens a batch so its cells arrive as a single
#: envelope and the server's one-transaction rule (inv. 15) makes the grid all-or-nothing; outside a
#: batch each `add` posts on its own.
_BATCH: list | None = None
_BATCH_BUILT: dict | None = None


def _build_cache_root() -> Path:
    """Where the Cython build venv and the .so cache live under the API transport (inv. 11).

    NOT the data root. That is the whole point: with `RUNQ_TRANSPORT=api` the client writes nothing
    under `experiments/`, so the coordinator's root can be mounted read-only and the ACL revoked."""
    env = os.environ.get("RUNQ_BUILD_CACHE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(xdg) / "fleet" / "build"


def _build_for_api(conn, code_tar: bytes, code_hash: str, entry_src: str | None,
                   git_sha: str) -> dict:
    """Build ship-ready bytes LOCALLY and return what the envelope needs — without touching the data
    root. Same compile as `build_and_store` (same recipe row, so one ABI still governs the fleet);
    only the cache location and the destination of the result differ."""
    rec = artifact_store.recipe(conn)
    packages = len(rec["bundle_compile_packages"] or [])
    # ASK FIRST, exactly as `build_and_store` checks the local store before building: the id is a
    # function of the inputs, so it is known before any compile. On a hit the coordinator's copy is
    # the one every box will ship, so its digest — not a fresh rebuild's — is the one to record.
    fmt = "compiled" if rec.get("bundle_compile", True) else "snapshot"
    bid = artifact_store.blob_id(code_hash, rec["bundle_compile_abi"],
                                 rec["bundle_compile_packages"], entry_src, fmt)
    held = api_client.ApiClient().blob_digest(bid)
    if held is not None:
        return {"blob_id": bid, "data": None, "sha256": held, "format": fmt,
                "compile_event": {"mode": "hit", "sec": 0.0, "git_sha": (git_sha or "")[:12],
                                  "packages": packages}}
    cache = _build_cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    data, fmt = artifact_store.build_ship_ready(
        code_tar, entry_source_path=entry_src, rec=rec, bundle_mod=bundle, experiments_root=cache)
    bid = artifact_store.blob_id(code_hash, rec["bundle_compile_abi"],
                                 rec["bundle_compile_packages"], entry_src, fmt)
    return {"blob_id": bid, "data": data, "sha256": artifact_store.digest(data), "format": fmt,
            "compile_event": {"mode": "miss", "sec": round(time.monotonic() - t0, 2),
                              "git_sha": (git_sha or "")[:12], "packages": packages}}


def _post(rows: list, built: dict, by: str, force: bool) -> int:
    """Push the blob (once) and the rows (one envelope). Prints the task ids `runq` always printed."""
    client = api_client.ApiClient()
    held = client.blob_digest(built["blob_id"])
    if held is None and built["data"] is not None:
        reply = client.put_blob(built["blob_id"], built["data"], built["sha256"])
        # Record what is PUBLISHED: if another client won the race to this id, its bytes are what
        # every box will hold, and the server says so in `sha256`.
        built["sha256"] = reply.get("sha256", built["sha256"])
        print(f"runq: uploaded ship artifact {built['blob_id']} "
              f"({len(built['data']) / 1e6:.1f} MB)", file=sys.stderr)
    else:
        if held is not None:
            built["sha256"] = held
        print(f"runq: reused ship artifact {built['blob_id']}", file=sys.stderr)
        built["compile_event"]["mode"] = "hit"
    reply = client.submit({
        "envelope_version": 1, "submission_id": str(uuid.uuid4()), "created_by": by,
        "blob_id": built["blob_id"], "code_sha256": built["sha256"],
        "compile_event": built["compile_event"], "force": bool(force), "tasks": rows})
    for tid in reply.get("task_ids", []):
        print(tid)
    return 0


def _finalize_via_api(a, row: dict, built: dict) -> int:
    """One task's tail under the API transport: batch it, or send it now."""
    global _BATCH_BUILT
    if _BATCH is not None:
        _BATCH.append(row)
        if _BATCH_BUILT is None:
            _BATCH_BUILT = built        # every cell shares one snapshot (ship-artifact-build inv. 4)
        return 0
    return api_client.run(_post, [row], built, a.by, getattr(a, "force", False))


def _finalize_task(conn, a: argparse.Namespace, *, entrypoint_label: str, entry_args: list[str],
                   config: dict, chash: str, ahash: str, git_sha: str, est_minutes: int,
                   resource_hint: str | None, resume_ckpt: str | None,
                   job_manifest_json: str | None, code_tar: bytes, code_hash: str) -> int:
    """Insert the task row + persist its code snapshot, keyed by the new task id. One tail shared by
    named `add`, `add <dir>`, and `submit`."""
    # --probe: a quick hypothesis test that blocks a decision. Priority resolved HERE (not at parse
    # time) because est_minutes is only known now — it may come from the learned per-entrypoint
    # default or the manifest, not just --est-minutes. `getattr` because the sweep path builds its
    # own Namespace (which always sets an explicit priority and carries no `probe`).
    probe = getattr(a, "probe", False)
    if probe and est_minutes > PROBE_MAX_MINUTES:
        print(f"runq add --probe: est_minutes {est_minutes} > {PROBE_MAX_MINUTES} — a job this long "
              f"is not a probe. A --probe task preempts running work, so mislabelling a sweep as one "
              f"evicts real jobs for hours. Queue it normally, or split out the single narrow read "
              f"that actually answers the question (see the project's working rules, 'Suspect a specific part?').",
              file=sys.stderr)
        return 2
    priority = a.priority if a.priority is not None else (PROBE_PRIORITY if probe else DEFAULT_PRIORITY)
    task_id = registry_db.new_task_id()
    now = registry_db.now_iso()
    # BUILD BEFORE INSERT (ship-artifact-build spec inv. 2/13/14). The queuer owns compilation, so a
    # build failure blows the whole task HERE and hands it back — nothing is inserted, no slot is
    # claimed, no box is rented, and the compiler's error lands in the terminal that typed the
    # command rather than hours later in a daemon nobody is watching. `build_and_store` raises
    # `BuildFailed`, which `main` turns into a non-zero exit; because every cell of a sweep shares
    # one snapshot, the first cell's failure aborts the sweep with ZERO cells queued (inv. 14).
    entry_src = entrypoints.entry_source_path(entrypoints.resolve(
        {"entrypoint": entrypoint_label, "job_manifest_json": job_manifest_json}))
    # The `compile` event MOVES HERE with the build (it is emitted per queue, not per ship, so the
    # dashboard's Compilation panel keeps its cold/warm timing series and hit-rate — now measuring
    # the queuer instead of the coordinator, which is where the work actually happens).
    # Shape is unchanged (`{mode, sec, git_sha, packages}`) so `compile_summary` needs no edit:
    # `miss` = a real build, `hit` = an existing blob reused.
    def _emit(mode, bid, sec):
        print(f"runq: {'built' if mode == 'miss' else 'reused'} ship artifact {bid}"
              f"{f' in {sec:.1f}s' if mode == 'miss' else ''}", file=sys.stderr)
        registry_db.log_event(conn, "compile", json.dumps(
            {"mode": mode, "sec": round(sec, 2), "git_sha": (git_sha or "")[:12],
             "packages": len(artifact_store.recipe(conn)["bundle_compile_packages"] or [])},
            separators=(",", ":")))

    if api_client.enabled():
        # THE TRANSPORT IS EXPLICIT AND THERE IS NO FALLBACK (inv. 10). Everything above this line
        # is a read or a local build; everything below it used to write the coordinator's data root.
        api_built = _build_for_api(conn, code_tar, code_hash, entry_src, git_sha)
        return _finalize_via_api(a, {
            "grp": a.group, "name": a.name, "entrypoint": entrypoint_label,
            "args_json": json.dumps(entry_args), "config_json": json.dumps(config),
            "config_hash": chash, "arm_hash": ahash, "git_sha": git_sha,
            "slots": (1 if a.slots is None else int(a.slots)), "est_minutes": est_minutes,
            "priority": priority, "max_retries": a.max_retries,
            "resource_hint_json": resource_hint, "resume_checkpoint": resume_ckpt,
            "job_manifest_json": job_manifest_json, "code_format": api_built["format"],
        }, api_built)
    built = artifact_store.build_and_store(
        conn, Path(a.db).parent, code_tar=code_tar, code_hash=code_hash,
        entry_source_path=entry_src, bundle_mod=bundle,
        on_reuse=lambda bid: _emit("hit", bid, 0.0),
        on_build=lambda bid, sec: _emit("miss", bid, sec))
    try:
        registry_db.insert_task(
            conn, id=task_id, created_at=now, created_by=a.by,  # resolved+validated in main (inv. 15)
            grp=a.group, name=a.name, entrypoint=entrypoint_label,
            args_json=json.dumps(entry_args), config_json=json.dumps(config), config_hash=chash,
            arm_hash=ahash, git_sha=git_sha, slots=(1 if a.slots is None else int(a.slots)),
            est_minutes=est_minutes,
            priority=priority, max_retries=a.max_retries, resource_hint_json=resource_hint,
            resume_checkpoint=resume_ckpt, job_manifest_json=job_manifest_json,
            code_blob=built["code_blob"], code_sha256=built["code_sha256"],
            code_format=built["code_format"])
    except sqlite3.IntegrityError as e:
        # ⛔ `(grp, name)` IS UNIQUE ACROSS **ALL** ROWS, INCLUDING `cancelled` AND `failed` ONES.
        # The dedup guard above already refuses a same-config re-add with a clear message and exit 3
        # — but `--force` is precisely the flag that SKIPS that guard, so a re-queue under a name
        # that has ever been used lands here instead and used to surface as a raw traceback with the
        # constraint text and no instruction. `cmd_sweep` has caught this since it was written; the
        # single-task path never did, and the single-task path is the one you reach for when
        # re-queueing a CANCELLED arm, which is exactly when the old name is still taken.
        # (2026-08-15: hit while repinning a paired scout off a box that could not admit it.)
        if "tasks.grp" not in str(e) and "tasks.name" not in str(e):
            raise
        prev = conn.execute("SELECT id, state FROM tasks WHERE grp=? AND name=?",
                            (a.group, a.name)).fetchone()
        was = f" (task {prev[0]}, state={prev[1]})" if prev else ""
        print(f"runq add: refused — the name {a.group}/{a.name} is ALREADY TAKEN{was}. "
              f"(grp, name) is unique across every row, cancelled and failed included, so --force "
              f"cannot reuse it. Queue under a new name (e.g. {a.name}b).", file=sys.stderr)
        return 2
    code_snapshot.persist(Path(a.db).parent, task_id, code_tar, code_hash)
    # The WRITER prunes (inv. 7), refcounted against open tasks — so the store can never evict the
    # blob a queued task is waiting to ship. That is the bug in the compile cache this replaces:
    # it was LRU-capped with no reference to live tasks at all. Keeps the coordinator blind to the
    # store's lifecycle, which is the point of the whole design.
    artifact_store.gc(Path(a.db).parent, artifact_store.live_blob_ids(conn),
                      _blob_keep_max(conn))
    # Discoverability at the point of use: docs only reach whoever read them, and the whole reason
    # --probe exists is that a decision-blocking read used to be smuggled onto the dev box to dodge
    # the queue. So when a job is short enough to BE a probe and took the default priority, say so
    # once. `a.priority is None` means nobody chose a priority — which also excludes `runq sweep`
    # (its cells always set one explicitly), so a grid never emits this per cell.
    if not probe and getattr(a, "priority", None) is None and est_minutes <= PROBE_MAX_MINUTES:
        print(f"runq add: hint — queued at default priority {DEFAULT_PRIORITY}. If this run's answer "
              f"BLOCKS a decision, re-queue it with --probe (priority {PROBE_PRIORITY}): it preempts "
              f"lower-priority work so it starts in minutes instead of behind a long job.",
              file=sys.stderr)
    print(task_id)
    return 0


def _resume_ckpt_or_error(a: argparse.Namespace, resume_flag: str | None,
                          who: str) -> tuple[str | None, int | None]:
    """Resolve --init-from: (abs path | None, error-exit-code | None). Requires the contract to
    declare a resume flag (named entry or manifest), matching the pre-manifest behavior."""
    if a.init_from is None:
        return None, None
    ckpt = os.path.abspath(a.init_from)
    if not os.path.isfile(ckpt):
        print(f"{who}: --init-from file not found: {ckpt}", file=sys.stderr)
        return None, 2
    if resume_flag is None:
        print(f"{who}: no resume flag declared — cannot --init-from", file=sys.stderr)
        return None, 2
    if api_client.enabled():
        seen_by_coordinator = _path_as_the_coordinator_sees_it(ckpt)
        if seen_by_coordinator is None:
            print(f"{who}: --init-from {ckpt} is outside the shared experiments root "
                  f"{registry_db.shared_experiments_root()}. Over the API transport only the PATH is "
                  f"sent, and the coordinator reads it on its own filesystem, where it can see nothing "
                  f"else of this machine: use a checkpoint the fleet pulled back (it lives under that "
                  f"root), or ship the file inside the code snapshot.", file=sys.stderr)
            return None, 2
        ckpt = seen_by_coordinator
    return ckpt, None


#: Where the coordinator's own containers mount the shared experiments root
#: (`fleet/coordinator/docker-compose.yml`: `${COORD_DATA}:/srv/fleet/experiments`). A queuer
#: on the API transport sees the SAME directory at `registry_db.shared_experiments_root()`.
COORDINATOR_EXPERIMENTS_ROOT = "/srv/fleet/experiments"


def _path_as_the_coordinator_sees_it(queuer_path: str) -> str | None:
    """A file under the shared experiments root, re-rooted to where the coordinator mounts that root;
    None for a file anywhere else.

    ⛔ Over the API transport `--init-from` sends a PATH, not bytes, and the dispatcher later does
    `Path(resume_checkpoint).read_bytes()` on its own filesystem. The queuer's spelling of the shared
    root (`/workspace/project/experiments` in the devcontainer) does not exist there, so every
    `--init-from` — even of a checkpoint the fleet itself had pulled back into that root — passed the
    queue-time check here and died at ship time with "resume checkpoint unreadable by the dispatcher"
    (twice on 2026-09-29; on 2026-09-20 the same shape took the dispatcher down for ~40 minutes).
    Re-rooting the path makes a pulled checkpoint usable as another task's starting point on ANY box,
    which is what `--init-from` is for; refusing everything else turns a ship-time failure into a
    queue-time one."""
    root = registry_db.shared_experiments_root().resolve()
    try:
        relative = Path(queuer_path).resolve().relative_to(root)
    except ValueError:
        return None
    return str(Path(os.environ.get("COORD_EXPERIMENTS_ROOT", COORDINATOR_EXPERIMENTS_ROOT)) / relative)


def _warn_if_no_coordinator(db: str) -> None:
    """Is anything actually DRIVING the registry we are about to queue into?

    ⛔ The failure this exists to stop is SILENT. `shared_experiments_root()` resolves to
    `<checkout>/experiments`, so every checkout gets a data root whether or not a coordinator drives
    it. After the 2026-09-17 cutover the live registry moved to tower, and the desktop kept a
    full, valid, STALE copy — so `runq add` there would succeed, print a task id, write a snapshot
    and a ship blob, and simply never run. Nothing in the output would look wrong.

    A registry being driven emits a `poll_cycle` event every ~30 s, so its absence is the cheapest
    possible liveness signal, and it needs no extra state to maintain.

    Warns rather than refuses: a legitimately new/empty registry, a deliberately isolated test root,
    and a coordinator that is briefly down are all real cases, and blocking them would be worse than
    the trap. Loud on stderr is enough to break the silence.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT t FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 1").fetchone()
        conn.close()
    except Exception:  # noqa: BLE001 — a brand-new registry has no events table yet
        return
    if not row or not row[0]:
        return
    import datetime as _dt
    try:
        last = _dt.datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return
    age_min = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds() / 60.0
    if age_min < 15:
        return
    print(
        f"runq: ⚠ the registry you are queueing into has seen no coordinator poll for "
        f"{age_min:.0f} min ({db}).\n"
        f"      Nothing may be driving it — work queued here would sit forever. Start one "
        f"against this data root: python fleet/dispatcher.py",
        file=sys.stderr)


def _refuse_if_dataroot_unwritable(db: str) -> int | None:
    """⛔ CAN THIS PROCESS WRITE THE DATA ROOT AT ALL? Refuse up front if not.

    Sibling of `_warn_if_no_coordinator`, and the opposite call: that one WARNS, because a registry
    nothing polls is sometimes legitimate. This one REFUSES, because an unwritable data root cannot
    possibly succeed — the registry row, the code snapshot, the ship blob and the Cython build venv
    all live under it.

    ⛔ THE FAILURE IT REPLACES ACTIVELY MISDIRECTS, which is why it is worth a preflight rather than
    letting the write fail where it happens. Measured 2026-09-17, first queue attempt from the
    devcontainer after the tower cutover: `runq add` got ~30 s into the build and died with

        build toolchain unavailable: build venv create failed:
          Permission denied: '<root>/.dispatcher/buildenv-py312'
        ... `uv python install 3.12` usually fixes it

    Every word after the colon is a true statement about the wrong problem, and the hint is advice
    that cannot work: the toolchain is fine, `uv` is installed, and the next agent following that
    hint spends the round on a Python install. The real cause is an ownership mismatch the repo
    already handles ONE layer up and not this one — `.devcontainer/.dataroot` binds the coordinator's
    live root (uid 1500, mode 755) into a container that runs as uid 1000, the same mismatch the
    Dockerfile fixes for `/srv/coord/repo.git` with `safe.directory`. The mount's own comment calls
    it "THE DATA ROOT the queuer writes to"; nothing made it writable.

    ⚠ THE MESSAGE POINTS AT `host_setup.sh` §6 RATHER THAN CARRYING ITS OWN RECIPE, and that is the
    correction from the first version of this guard. It printed a `setfacl` line it had invented,
    which the repo then provisioned differently and for stated reasons — a shared group plus setgid,
    because an ACL is invisible in `ls -l`, does not survive a `cp`, and decays when the tree is
    rewritten. An error message that recommends a mechanism the repo rejected is worse than one that
    names where the answer lives, so this names the script and prints the group the root ACTUALLY
    carries.

    Returns an exit code to propagate, or None when the root is writable.
    

    ⚠ THIS PATH IS THE `local` TRANSPORT ONLY (remote-submit inv. 21). On the host that owns
    the live root an unwritable data root is the DESIRED state — `RUNQ_TRANSPORT=api` sends the
    task to the coordinator instead — so this guard is not consulted there, and its `setfacl`
    advice would be exactly the wrong fix.
    """
    root = Path(db).parent
    dbf = Path(db)
    if os.access(root, os.W_OK) and (not dbf.exists() or os.access(dbf, os.W_OK)):
        return None
    try:
        st = os.stat(root)
        owner = "uid=%d gid=%d mode=%o" % (st.st_uid, st.st_gid, st.st_mode & 0o777)
        grp_id = str(st.st_gid)
        # ⚠ name the group the root ACTUALLY carries, not a hardcoded `coord`. On a host provisioned
        # with a different `--user` the printed commands must still be the right ones to run.
        try:
            import grp as _grp
            grp_name = _grp.getgrgid(st.st_gid).gr_name
        except (KeyError, ImportError):
            grp_name = grp_id
    except OSError:
        owner, grp_id, grp_name = "unreadable", "?", "coord"
    print(
        "runq add: ⛔ the data root is NOT WRITABLE by this process — nothing was queued.\n"
        "      root:  %s  (%s)\n"
        "      me:    uid=%d gid=%d\n"
        "      The registry row, the code snapshot, the ship blob and the Cython build venv all\n"
        "      live under that root, so this cannot be worked around from here. It is an OWNERSHIP\n"
        "      mismatch, not a toolchain or a Python problem: `.devcontainer/.dataroot` binds the\n"
        "      coordinator's live root into a container running as a different uid.\n"
        "      THE FIX IS PROVISIONED, IN TWO HALVES — `fleet/coordinator/host_setup.sh`\n"
        "      section 6 (a shared GROUP plus setgid, deliberately not per-uid ACLs) plus the\n"
        "      matching group in `.devcontainer/Dockerfile`. A supplementary group is resolved\n"
        "      from the CONTAINER's /etc/group, so either half alone does nothing. On the host:\n"
        "        sudo bash fleet/coordinator/host_setup.sh --dev-user <you>\n"
        "      or by hand, for group `%s` (gid %s):\n"
        "        sudo usermod -aG %s <you> && sudo chgrp -R %s %s\n"
        "        sudo chmod -R g+rwX %s && sudo find %s -type d -exec chmod g+s {} +\n"
        "      ⚠ setgid on the DIRECTORIES is the load-bearing half: without it, files the\n"
        "      dispatcher creates land in ITS group and this side loses write access again,\n"
        "      silently, later. Until then, queue from a shell that owns the root." % (
            root, owner, os.getuid(), os.getgid(),
            grp_name, grp_id, grp_name, grp_name, root, root, root),
        file=sys.stderr)
    return 2


def cmd_add(a: argparse.Namespace, entry_args: list[str]) -> int:
    if not a.group or not a.name:
        print("runq add: --group and --name must be non-empty", file=sys.stderr)
        return 2
    _warn_if_no_coordinator(a.db)
    # Under the API transport an UNWRITABLE root is the expected, desired state (remote-submit
    # inv. 21): the client writes nothing there, so refusing on it would refuse the whole feature.
    if not api_client.enabled():
        blocked = _refuse_if_dataroot_unwritable(a.db)
        if blocked is not None:
            return blocked
    if bool(a.vram_per_lane_gb is None) != bool(a.cores_per_lane is None):
        print("runq add: --vram-per-lane-gb and --cores-per-lane must be given together",
              file=sys.stderr)
        return 2
    if bool(a.entrypoint) == bool(a.config):
        print("runq add: exactly one of --entrypoint <name> or --config <path> is required",
              file=sys.stderr)
        return 2
    return _add_config(a, entry_args) if a.config else _add_named(a, entry_args)


def _add_named(a: argparse.Namespace, entry_args: list[str]) -> int:
    """Legacy path: a named entrypoint resolved from the coordinator's `entrypoints.py` table.
    Unchanged behavior — every existing `runq add --entrypoint X` still works exactly as before."""
    try:
        est_minutes, est_source = est_defaults.resolve_est_minutes(a.est_minutes, a.entrypoint)
    except ValueError as e:
        print(f"runq add: {e}", file=sys.stderr)
        return 2
    if est_source == "learned":
        print(f"runq add: using learned est_minutes={est_minutes} for {a.entrypoint}",
              file=sys.stderr)
    try:
        entry = entrypoints.get(a.entrypoint)
        config = _run_identity_handshake(entry, entry_args)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    chash, ahash = config_hash(config), arm_hash(config)
    conn = registry_db.connect(a.db)
    guard = _dedupe_guard(conn, a, chash, ahash)
    if guard is not None:
        return guard
    _hint = {}
    if a.vram_per_lane_gb is not None:
        _hint = {"vram_per_lane_gb": a.vram_per_lane_gb, "cores_per_lane": a.cores_per_lane}
    if getattr(a, "box", None):          # `--box` works on the named-entrypoint path too
        _hint["box"] = str(a.box)
    if getattr(a, "colocate", None):     # ...and so does `--colocate` (dispatcher inv. 4g)
        _hint["colocate"] = str(a.colocate)
    if getattr(a, "force_box", False):   # ...and `--force-box` (dispatcher inv. 4i)
        _hint["force_box"] = True
    conflict = _hint_conflict(_hint) or _force_box_error(a, _hint)
    if conflict:
        print(f"runq add: {conflict}", file=sys.stderr)
        return 2
    resource_hint = json.dumps(_hint) if _hint else None
    resume_ckpt, err = _resume_ckpt_or_error(a, entry.resume_flag, "runq add")
    if err is not None:
        return err
    # Snapshot THIS worktree (committed + uncommitted, .gitignore-aware) so the dispatcher ships
    # exactly what's on disk here — no commit required, no git_sha reachability (code-snapshot inv. 6).
    try:
        snap = code_snapshot.make_snapshot(ROOT)
    except code_snapshot.SnapshotError as e:
        print(f"runq add: {e}", file=sys.stderr)
        return 2
    return _finalize_task(
        conn, a, entrypoint_label=a.entrypoint, entry_args=entry_args, config=config,
        chash=chash, ahash=ahash, git_sha=_git_sha(), est_minutes=est_minutes,
        resource_hint=resource_hint, resume_ckpt=resume_ckpt, job_manifest_json=None,
        code_tar=snap.code_tar, code_hash=snap.code_hash)


#: Re-exported so `runq.RESUME_REQUIRED_OVER_MINUTES` keeps resolving; the constant now lives with
#: the contract it belongs to, beside `check_submittable`.
RESUME_REQUIRED_OVER_MINUTES = job_manifest.RESUME_REQUIRED_OVER_MINUTES


def _check_resume_contract(a: argparse.Namespace, manifest: job_manifest.JobManifest,
                           est_minutes: int) -> str | None:
    """Refuse to queue a long manifest job that cannot resume — at the point of SPEND.

    The named-entrypoint table carried `resume_flag` and the coordinator honoured it; the
    artifact-agnostic manifest path moved that declaration into each config's own `job` section, where it
    became easy to simply omit. It was omitted, and nothing downstream noticed: `native.train_m49_
    curriculum_ab` ran 212 tasks over 22 hours with `resume_flag=None`, so the dispatcher never
    pulled a checkpoint and never re-appended `--init-from`. Eight died with nothing to restart
    from.

    The verdict itself is `job_manifest.check_submittable` — ONE function, so this gate and the
    repo-wide audit cannot drift apart. `--no-resume` is refused rather than honoured: an opt-out
    that lives on the invocation evaporates the moment the command returns, which is exactly how a
    deliberate opt-out became indistinguishable from a forgotten one."""
    if (getattr(a, "no_resume", None) or "").strip():
        return ("--no-resume is no longer accepted — the opt-out must be DECLARED IN THE CONFIG so "
                "it survives the command that queued it. Put the reason in the `job` section:\n"
                '      "resume": {"none": "<why a restart-from-scratch is acceptable here>"}\n'
                "    Then anyone reading the config — or auditing every committed config at once — "
                "sees the same decision the queue-time gate saw.")
    return job_manifest.check_submittable(manifest, est_minutes)


def _snapshot_root_for(config_path: Path) -> Path:
    """The tree to ship for a config. The git toplevel that CONTAINS the config when there is one,
    else the config's own directory.

    v1 snapshotted the `--job` directory itself, which is why `jobs/<exp>/job.json` dirs were
    unusable: a dir holding one file shipped a one-file tree with no `src/`. Rooting at the repo
    means the config can live anywhere sensible (`configs/continual/…`) and still ship the code."""
    d = config_path.parent
    try:
        out = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return d.resolve()


def _add_config(a: argparse.Namespace, entry_args: list[str]) -> int:
    """Self-describing path: a trainer config whose reserved `job` section is the run contract. The
    coordinator needs no entrypoints.py entry. Snapshot is git-optional."""
    config_path = Path(a.config).resolve()
    try:
        raw = job_manifest.read_config(config_path)
        if raw is None:
            print(f"runq add: {config_path} has no '{job_manifest.JOB_SECTION}' section — a queued "
                  f"config must declare its own run contract (job-artifact-contract spec v2); "
                  f"there is no default to fall back to.", file=sys.stderr)
            return 2
        manifest = job_manifest.parse(raw)
    except job_manifest.JobManifestError as e:
        print(f"runq add: invalid config: {e}", file=sys.stderr)
        return 2
    snap_root = _snapshot_root_for(config_path)
    try:
        rel_config = config_path.relative_to(snap_root).as_posix()
    except ValueError:
        print(f"runq add: {config_path} is not under its snapshot root {snap_root}",
              file=sys.stderr)
        return 2
    # The config path the BOX will use is tar-relative, and it goes in front of the caller's own
    # `--set` overrides so those still win (harness applies --set over the loaded file).
    entry_args = ["--config", rel_config, *entry_args]
    a.slots = _manifest_slots(a, manifest)
    est_minutes, resource_hint, err = _manifest_est_and_hint(a, manifest)
    if err:
        print(f"runq add: {err}", file=sys.stderr)
        return 2
    err = _check_resume_contract(a, manifest, est_minutes)
    if err:
        print(f"runq add: {err}", file=sys.stderr)
        return 2
    try:
        config = _run_identity_handshake(job_manifest.to_entrypoint(manifest), entry_args,
                                         root=snap_root)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    chash, ahash = config_hash(config), arm_hash(config)
    conn = registry_db.connect(a.db)
    guard = _dedupe_guard(conn, a, chash, ahash)
    if guard is not None:
        return guard
    resume_ckpt, err = _resume_ckpt_or_error(a, manifest.resume_flag, "runq add")
    if err is not None:
        return err
    try:
        snap = code_snapshot.make_snapshot(snap_root, hoist=rel_config)  # git-optional (inv. 7)
    except code_snapshot.SnapshotError as e:
        print(f"runq add: {e}", file=sys.stderr)
        return 2
    return _finalize_task(
        conn, a, entrypoint_label=_manifest_label(manifest.run), entry_args=entry_args,
        config=config, chash=chash, ahash=ahash, git_sha=_git_sha(snap_root), est_minutes=est_minutes,
        resource_hint=resource_hint, resume_ckpt=resume_ckpt,
        job_manifest_json=json.dumps(job_manifest.to_dict(manifest)),
        code_tar=snap.code_tar, code_hash=snap.code_hash)


def entrypoint_ignoring_set(config_path: str) -> str | None:
    """The `.py` entrypoint in a config's `job.run` that cannot consume `--set`, or None if fine.

    ⚠ STATIC, AND IT FAILS OPEN. The reliable test is the `--print-run-identity` handshake, which
    imports torch and costs more than the entire queue operation; this reads the script's source
    instead. It returns None -- "no objection" -- for a MODULE entrypoint (`-m pkg.train`, which
    reaches `shared.infra.harness` and does accept `--set`), for a config it cannot parse, and for a
    script it cannot locate or read. A queue guard that blocked on everything it could not judge would
    be worse than the bug it prevents.

    ⛔ THREE WAYS AN ENTRYPOINT LEGITIMATELY GETS `--set`, and the check must know all three or it
    manufactures false refusals: it declares the flag itself; it borrows `probe_argparser`
    (`scripts/diagnostics/_probe_job.py`); or it defers to `run_trainer` / `shared.infra.harness`,
    which parses argv on its behalf and names the flag NOWHERE in the calling script. Matching on the
    bare string `shared.infra` would be wrong in the other direction -- `sparse_pc_ladder.py` imports
    `run_identity` while accepting no overrides at all.

    ⚠ `job.run` paths are relative to the SNAPSHOT ROOT, which is the repo root for a config under
    `configs/**` and the job directory itself for a self-contained job dir. Neither is knowable from
    the config path alone, so resolve against the CWD, then the config's own directory, then walk up.
    """
    try:
        with open(config_path) as f:
            run = (json.load(f).get("job") or {}).get("run") or []
    except (OSError, json.JSONDecodeError):
        return None
    for tok in run:
        if not (isinstance(tok, str) and tok.endswith(".py")):
            continue
        base = os.path.dirname(os.path.abspath(config_path))
        cands = [tok]
        for _ in range(6):
            cands.append(os.path.join(base, tok))
            parent = os.path.dirname(base)
            if parent == base:
                break
            base = parent
        src = None
        for c in cands:
            try:
                with open(c, encoding="utf-8", errors="replace") as f:
                    src = f.read()
                break
            except OSError:
                continue
        if src is None:
            return None                      # cannot locate it ⇒ cannot judge it ⇒ do not block
        if any(m in src for m in ('"--set"', "'--set'", "probe_argparser",
                                  "run_trainer", "shared.infra.harness")):
            return None
        return tok
    return None


def cmd_sweep(a: argparse.Namespace) -> int:
    """Expand a sweep.json into N manifest adds (docs/specs/runq-sweep.spec.md). Each cell rides
    the ordinary `add --config` path (`--set` overrides as entry_args), so identity/dedupe/snapshot
    behave exactly as hand-issued adds; duplicate cells are skipped, making re-runs idempotent."""
    import sqlite3
    try:
        with open(a.sweep_json) as f:
            sweep = sweep_expand.parse_sweep(json.load(f))
        cells = sweep_expand.expand(sweep)
    except (OSError, json.JSONDecodeError, sweep_expand.SweepError) as e:
        print(f"runq sweep: {e}", file=sys.stderr)
        return 2
    group = a.group or sweep.get("group")
    if not group:
        print("runq sweep: no group ('group' in the sweep file or --group)", file=sys.stderr)
        return 2
    if len(cells) > a.max_cells:
        print(f"runq sweep: {len(cells)} cells exceeds --max-cells {a.max_cells} "
              "(fat-finger guard; raise it if intended)", file=sys.stderr)
        return 2

    # ⛔ AN ENTRYPOINT THAT CANNOT PARSE `--set` TURNS A SWEEP INTO N COPIES OF ITS BASE CONFIG.
    # Every cell rides `add --config` carrying its axis as `--set path=value` entry_args (see
    # `sweep_expand.cell_args`). An argparser that does not accept `--set` DROPS them in silence: each
    # cell completes, reports a number, and the axis was never varied -- then identity-dedupe reports
    # the siblings as `skipped-duplicate`, which reads like idempotence rather than a defect. So the
    # failure presents as "my sweep ran and the arms all agree", which is the most expensive shape a
    # bug can have here.
    # ⚠ MEASURED 2026-09-12, and it is not an edge case: `sparse_pc_ladder.py` uses `parse_known_args`
    # and its `--print-run-identity` handshake was BYTE-IDENTICAL with and without `--set`, and 5 of
    # the 10 `.py` entrypoints named in `configs/**` `job.run` cannot consume `--set` at all. A 4-arm
    # `norm_mode` sweep would have queued one real arm and three duplicates.
    # ⇒ Refuse, and name both fixes. `--force` is the escape for an entrypoint that takes overrides by
    # some route this static check cannot see.
    if not a.force:
        _bad = entrypoint_ignoring_set(sweep["config"])
        if _bad:
            print(f"runq sweep: {_bad} does not accept `--set`, so all {len(cells)} cells would run "
                  f"the BASE config with the axis SILENTLY UNVARIED (siblings then report as "
                  f"'skipped-duplicate', which looks like idempotence). Fix either end: give that "
                  f"entrypoint a `--set` handler (see `shared.infra.harness`, or `probe_argparser` in "
                  f"`scripts/diagnostics/_probe_job.py`), or express the arms in the config's own "
                  f"`arms` list and `runq add` it as ONE cell — which also pairs them harder, since "
                  f"one cell is one process on one box. --force overrides.", file=sys.stderr)
            return 2

    # Sibling co-location, ON BY DEFAULT (owner directive 2026-08-16, dispatcher inv. 4g): all arms
    # of one paired seed land on ONE box, different seeds are free to land anywhere. A sweep is the
    # only place that KNOWS which cells are the same seed, which is why the default lives here and
    # not in `add`.
    try:
        keys = ({} if (a.no_colocate or sweep.get("colocate") is False)
                else sweep_expand.colocate_keys(
                    group, cells, by=a.colocate_by or (
                        sweep.get("colocate") if isinstance(sweep.get("colocate"), (str, list))
                        else None)))
    except sweep_expand.SweepError as e:
        print(f"runq sweep: {e}", file=sys.stderr)
        return 2
    if a.box and keys:
        print("runq sweep: --box and co-location are mutually exclusive — --box already forces "
              "every cell onto one named machine. Add --no-colocate if that is really what you "
              "want.", file=sys.stderr)
        return 2
    sizes: dict[str, int] = {}
    for k in keys.values():
        sizes[k] = sizes.get(k, 0) + 1
    for k in sorted(sizes):
        print(f"[sweep] colocate {k}: {sizes[k]} cell(s) share one box")
    # A group cannot run wider than the box it pins, so a big group is a REAL serialisation cost and
    # the operator should see it before spending, not infer it from a slow queue.
    big = sorted(k for k, n in sizes.items() if n > COLOCATE_WARN_CELLS)
    if big:
        print(f"runq sweep: warning — colocation group(s) {big} exceed {COLOCATE_WARN_CELLS} cells. "
              f"Every member runs on ONE box, so anything past that box's lane count SERIALISES. "
              f"If these cells are not a paired comparison, pass --no-colocate; if only some axis "
              f"pairs them, name it with --colocate-by.", file=sys.stderr)

    if a.dry_run:
        for name, overrides in cells:
            colo = f" colocate={keys[name]}" if name in keys else ""
            print(f"[sweep] {name}: DRY{colo} {' '.join(sweep_expand.cell_args(overrides))}")
        print(f"[sweep] dry-run: {len(cells)} cells, nothing enqueued")
        return 0

    # Under the API transport every cell's row is COLLECTED and the grid arrives as ONE envelope,
    # so the server's single transaction (inv. 15) makes it all-or-nothing: a cell that trips dedupe
    # leaves the registry untouched rather than stranding a partial grid someone must hand-cancel.
    # Locally the behaviour is unchanged — per-cell inserts, abort-on-failure.
    global _BATCH, _BATCH_BUILT
    batching = api_client.enabled()
    if batching:
        _BATCH, _BATCH_BUILT = [], None

    queued = skipped = 0
    for name, overrides in cells:
        cell_ns = argparse.Namespace(
            db=a.db, group=group, name=name, entrypoint=None, config=sweep["config"],
            est_minutes=a.est_minutes if a.est_minutes is not None else sweep.get("est_minutes"),
            priority=a.priority if a.priority is not None else sweep.get("priority", 50),
            slots=sweep.get("slots"), max_retries=sweep.get("max_retries", 3),
            by=a.by, force=a.force, vram_per_lane_gb=None, cores_per_lane=None, init_from=None,
            box=a.box, colocate=keys.get(name))
        try:
            rc = cmd_add(cell_ns, sweep_expand.cell_args(overrides))
        except sqlite3.IntegrityError:
            print(f"runq sweep: {name}: name already taken in group {group!r} by a DIFFERENT "
                  "config (auto-name collision) — aborting", file=sys.stderr)
            return 2
        if rc == 0:
            queued += 1
            print(f"[sweep] {name}: queued" + (f" [colocate {keys[name]}]" if name in keys else ""))
        elif rc == 3:
            skipped += 1
            print(f"[sweep] {name}: skipped-duplicate")
        else:
            if batching:
                _BATCH, _BATCH_BUILT = None, None   # nothing was sent, so nothing is queued
            print(f"runq sweep: {name}: add failed (exit {rc}) — aborting remaining cells "
                  f"({queued} queued stay; re-run after the fix skips them)", file=sys.stderr)
            return rc
    if batching:
        rows, built, _BATCH, _BATCH_BUILT = _BATCH, _BATCH_BUILT, None, None
        if rows:
            rc = api_client.run(_post, rows, built, a.by, a.force)
            if rc != 0:
                print(f"runq sweep: the coordinator refused the grid (exit {rc}) — NOTHING was "
                      f"queued, which is the point: fix it and re-run once it is right.",
                      file=sys.stderr)
                return rc
            queued, skipped = len(rows), len(cells) - len(rows)
    print(f"[sweep] queued={queued} skipped={skipped}")
    return 0


def cmd_colocate(a: argparse.Namespace) -> int:
    """Report — and with `--verify`, GATE ON — whether each sibling group actually ran together.

    ⛔ THIS IS THE HALF THAT MAKES THE FEATURE HONEST. The dispatcher co-locates BEST-EFFORT: if the
    pinned box is torn down or lost mid-campaign the group re-pins rather than wedging forever, so
    "I asked for co-location" is not the same claim as "the arms were co-located". A published
    campaign once co-located only 2 of its 3 pairs and nothing in the outputs said so.

    ⚠ Reads `registry_db.boxes_started_on` — the distinct `start` EVENTS — never `tasks.instance_id`,
    which holds only the LATEST placement and so hides a requeued arm's first box. That makes a split
    invisible exactly where it is real, since a requeued arm is also the one with `resumes > 0`
    (`paired_seed_diff.boxes_for`'s scar; same read, same function).

    Verdicts: `OK` (every member ran on ONE box, the same one) · `SPLIT` (the group spans two or more
    boxes — including a single arm requeued onto a second, which is not paired with itself either;
    a control that collapsed on the other machine manufactures a win) · `PENDING` (nothing started
    yet)."""
    conn = registry_db.connect(a.db)
    if a.release:
        pin = registry_db.colocation_pins(conn).get(a.release)
        registry_db.unpin_colocation(conn, a.release)
        print(f"runq colocate: released {a.release!r}"
              + (f" (was pinned to box {pin['instance_id']})" if pin else " (no pin was recorded)"))
        return 0
    pins = registry_db.colocation_pins(conn)
    groups = registry_db.colocation_members(conn, key=a.key, grp=a.group)
    report, split = [], []
    for key in sorted(groups):
        members = [(m, registry_db.boxes_started_on(conn, m)) for m in groups[key]]
        placed = sorted({b for _m, boxes in members for b in boxes})
        verdict = "PENDING" if not placed else ("OK" if len(placed) == 1 else "SPLIT")
        if verdict == "SPLIT":
            split.append(key)
        report.append({
            "key": key, "verdict": verdict, "pinned_to": (pins.get(key) or {}).get("instance_id"),
            "instances": placed,
            "members": [{"id": m["id"], "grp": m["grp"], "name": m["name"], "state": m["state"],
                         "boxes": list(boxes)} for m, boxes in members]})
    if a.json:
        print(json.dumps(report))
    else:
        for g in report:
            print(f"{g['key']}  {g['verdict']:<8} pinned={g['pinned_to']} "
                  f"boxes={','.join(g['instances']) or '-'}  members={len(g['members'])}")
            for m in g["members"]:
                print(f"    {m['id']}  {m['state']:<12} {m['grp']}/{m['name']}  "
                      f"boxes={','.join(m['boxes']) or '-'}")
        if not report:
            print("runq colocate: no colocation groups match", file=sys.stderr)
    if a.verify and not report:
        sel = " ".join(filter(None, [f"--group {a.group}" if a.group else "",
                                     f"--key {a.key}" if a.key else ""])) or "(no selector)"
        print(f"runq colocate --verify: {sel} matched NO colocation group, so nothing was verified. "
              f"An empty selector is not a pass — it is a check that never ran, and reporting a "
              f"paired verdict on it claims an audit that did not happen. Note --group takes the "
              f"TASK GROUP ('bedknob'), not the colocation KEY ('bedknob:seeds1'); pass the key to "
              f"--key instead. Run `runq colocate` with no selector to see the live keys.",
              file=sys.stderr)
        return 2
    if a.verify and split:
        print(f"runq colocate: {len(split)} group(s) SPLIT ACROSS BOXES — {split}. Their arms were "
              f"NOT measured on the same machine, so paired deltas over them are not paired. Do not "
              f"report a verdict from these without re-running the split arms together.",
              file=sys.stderr)
        return 1
    return 0


def cmd_submit(a: argparse.Namespace, entry_args: list[str]) -> int:
    """Submit a PRE-BUILT artifact: a gzipped code tar (git-archive layout). `--config` names the
    config's path INSIDE the tar; its `job` section is the run contract. No directory, no git,
    nothing run locally — dedupe is content-addressed (inv. 12)."""
    if not a.group or not a.name:
        print("runq submit: --group and --name must be non-empty", file=sys.stderr)
        return 2
    if bool(a.vram_per_lane_gb is None) != bool(a.cores_per_lane is None):
        print("runq submit: --vram-per-lane-gb and --cores-per-lane must be given together",
              file=sys.stderr)
        return 2
    art = Path(a.artifact)
    if not art.is_file():
        print(f"runq submit: artifact not found: {art}", file=sys.stderr)
        return 2
    code_tar = art.read_bytes()
    try:
        raw = job_manifest.read_tar(code_tar, a.config)
        if raw is None:
            print(f"runq submit: {a.config} (inside {art}) has no "
                  f"'{job_manifest.JOB_SECTION}' section — a queued config must declare its own "
                  f"run contract.", file=sys.stderr)
            return 2
        manifest = job_manifest.parse(raw)
    except job_manifest.JobManifestError as e:
        print(f"runq submit: {e}", file=sys.stderr)
        return 2
    # Same as the `add --config` path: the box loads the named config, then the caller's --set wins.
    entry_args = ["--config", a.config, *entry_args]
    a.slots = _manifest_slots(a, manifest)
    est_minutes, resource_hint, err = _manifest_est_and_hint(a, manifest)
    if err:
        print(f"runq submit: {err}", file=sys.stderr)
        return 2
    err = _check_resume_contract(a, manifest, est_minutes)
    if err:
        print(f"runq submit: {err}", file=sys.stderr)
        return 2
    resume_ckpt, err = _resume_ckpt_or_error(a, manifest.resume_flag, "runq submit")
    if err is not None:
        return err
    code_hash = hashlib.sha256(code_tar).hexdigest()  # content address of the provided artifact
    config, chash, ahash = _content_identity(code_hash, manifest.run, entry_args)
    conn = registry_db.connect(a.db)
    guard = _dedupe_guard(conn, a, chash, ahash)
    if guard is not None:
        return guard
    return _finalize_task(
        conn, a, entrypoint_label=_manifest_label(manifest.run), entry_args=entry_args,
        config=config, chash=chash, ahash=ahash, git_sha="", est_minutes=est_minutes,
        resource_hint=resource_hint, resume_ckpt=resume_ckpt,
        job_manifest_json=json.dumps(job_manifest.to_dict(manifest)),
        code_tar=code_tar, code_hash=code_hash)


PROBE_WAIT_S = 90
KEY_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def cmd_box_key(a: argparse.Namespace) -> int:
    """`runq box key <id-or-label> <NAME|--clear>` — choose the ssh identity for ONE box.

    The registry owns this now (remote-submit inv. 22a): the dispatcher writes its own ssh config
    from these rows each poll, so adding a box never needs a root-side config edit, and a box's
    address can never desync from the key used to reach it. Unset ⇒ the fleet key."""
    conn = registry_db.connect(a.db)
    target = str(a.box)
    row = conn.execute("SELECT id, label FROM instances WHERE CAST(id AS TEXT)=? OR label=?",
                       (target, target)).fetchone()
    if row is None:
        print(f"runq box key: no instance with id or label {target!r}", file=sys.stderr)
        return 2
    key = f"ssh_key_i{row['id']}"
    if a.clear:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()
        print(f"runq box key: {row['label']} now uses the fleet key", file=sys.stderr)
        return 0
    if not a.name:
        cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        print(json.loads(cur["value"]) if cur else "(fleet key)")
        return 0
    if not KEY_NAME_RE.fullmatch(a.name):
        print(f"runq box key: {a.name!r} is a key NAME in the coordinator's ~/.ssh, not a path",
              file=sys.stderr)
        return 2
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(a.name)))
    conn.commit()
    print(f"runq box key: {row['label']} -> ~/.ssh/{a.name} (applied on the next poll)",
          file=sys.stderr)
    return 0


def cmd_box(a: argparse.Namespace) -> int:
    """`runq box probe <id-or-label>` — ask the COORDINATOR to check a box now (spec:
    `docs/specs/remote-submit.spec.md` inv. 19a/19b, still a draft on branch `spec/remote-submit`). Writes the one-shot `probe_request_i<id>` row the dispatcher consumes on its next poll,
    then (with --wait) blocks for the `box_probe` event it logs.

    Exits non-zero when the box is unreachable, and PRINTS THE SSH ERROR: `Permission denied
    (publickey)`, `Could not resolve hostname` and a timeout are three different faults with three
    different fixes, and until this existed the registry recorded none of them (23-Q1)."""
    if getattr(a, "box_cmd", "probe") == "key":
        return cmd_box_key(a)
    conn = registry_db.connect(a.db)
    target = str(a.box)
    row = conn.execute(
        "SELECT id, label, state, ssh_host, ssh_port FROM instances WHERE CAST(id AS TEXT)=? OR label=?",
        (target, target)).fetchone()
    if row is None:
        print(f"runq box probe: no instance with id or label {target!r}", file=sys.stderr)
        return 2
    iid = row["id"]
    since = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    if api_client.enabled():
        # The data root is read-only on the API transport (remote-submit inv. 20): the request row is
        # written by the coordinator's `/v1/boxes/<box>/probe`; the wait below only READS events.
        rc = api_client.run(api_client.ApiClient().box, row["label"], "probe")
        if rc:
            return rc
    else:
        conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"probe_request_i{iid}", json.dumps({"at": registry_db.now_iso()})))
        conn.commit()
    print(f"runq box probe: queued for instance {iid} ({row['label']}, state={row['state']}, "
          f"registered {row['ssh_host']}:{row['ssh_port']}) — the coordinator consumes it on its "
          f"next poll", file=sys.stderr)
    if not a.wait:
        return 0
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        ev = conn.execute("SELECT detail FROM events WHERE event='box_probe' AND instance_id=? "
                          "AND seq > ? ORDER BY seq DESC LIMIT 1", (iid, since)).fetchone()
        if ev is not None:
            try:
                d = json.loads(ev["detail"])
            except (ValueError, json.JSONDecodeError):
                print(ev["detail"])
                return 1
            print(json.dumps(d, indent=2, sort_keys=True))
            return 0 if d.get("reachable") else 1
        time.sleep(3)
    print(f"runq box probe: no box_probe event within {a.timeout}s — is the coordinator polling? "
          f"(`fleet/coordinator/roll_remote.sh status`)", file=sys.stderr)
    return 4


def _row_to_dict(row) -> dict:
    return dict(row)


def cmd_ls(a: argparse.Namespace) -> int:
    conn = registry_db.connect(a.db)
    for s in a.state or []:
        if s not in registry_db.LEGAL_STATES:
            print(f"runq ls: unknown --state {s!r}", file=sys.stderr)
            return 2
    rows = registry_db.list_tasks(conn, states=a.state or None, grp=a.group)
    if a.json:
        print(json.dumps([_row_to_dict(r) for r in rows]))
    else:
        for r in rows:
            print(f"{r['id']}  {r['state']:<14} {r['grp']}/{r['name']}  "
                  f"prio={r['priority']} entrypoint={r['entrypoint']}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    conn = registry_db.connect(a.db)
    row = registry_db.get_task(conn, a.task_id)
    if row is None:
        print(f"runq show: no such task {a.task_id}", file=sys.stderr)
        return 2
    events = [dict(e) for e in registry_db.get_events(conn, a.task_id)]
    payload = {"task": _row_to_dict(row), "events": events}
    if a.json:
        print(json.dumps(payload))
    else:
        print(json.dumps(payload, indent=2))
    return 0


def cmd_cancel(a: argparse.Namespace) -> int:
    if api_client.enabled():
        return api_client.run(api_client.ApiClient().cancel, a.task_id, a.reason,
                              getattr(a, "by", "") or "")
    conn = registry_db.connect(a.db)
    result = registry_db.cancel_task(conn, a.task_id, a.reason)
    if result.reason == "not_found":
        print(f"runq cancel: no such task {a.task_id}", file=sys.stderr)
        return 2
    if result.reason == "already_cancelling":
        print(f"runq cancel: {a.task_id} is already cancelling")
        return 0
    if result.reason == "terminal":
        print(f"runq cancel: {a.task_id} is already terminal — nothing to cancel", file=sys.stderr)
        return 4
    if not result.ok:
        print(f"runq cancel: could not cancel ({result.reason})", file=sys.stderr)
        return 4
    # queued/claimed cancel immediately; an in-flight task goes to `cancelling` and the dispatcher
    # stops its worker (writes CANCEL, awaits CANCELLED) before it reaches `cancelled`.
    state = registry_db.get_task(conn, a.task_id)["state"]
    if state == "cancelling":
        print(f"runq cancel: {a.task_id} — cancellation requested; the dispatcher will stop the "
              "worker and mark it cancelled")
    else:
        print(f"runq cancel: {a.task_id} cancelled")
    return 0


def cmd_dupes(a: argparse.Namespace) -> int:
    conn = registry_db.connect(a.db)
    if a.of:
        row = registry_db.get_task(conn, a.of)
        if row is None:
            print(f"runq dupes: no such task {a.of}", file=sys.stderr)
            return 2
        h = row["arm_hash"] if a.arm else row["config_hash"]
    else:
        h = a.hash
    rows = (registry_db.find_arm_matches(conn, h) if a.arm
            else [r for r in registry_db.list_tasks(conn) if r["config_hash"] == h])
    if a.json:
        print(json.dumps([_row_to_dict(r) for r in rows]))
    else:
        for r in rows:
            print(f"{r['id']}  {r['state']:<14} {r['grp']}/{r['name']}")
    return 0


def cmd_rate(a: argparse.Namespace) -> int:
    conn = registry_db.connect(a.db)
    r = registry_db.rate(conn)
    print(json.dumps({"rate": r}) if a.json else r)
    return 0


def cmd_spend(a: argparse.Namespace) -> int:
    conn = registry_db.connect(a.db)
    s = registry_db.spend(conn, since=a.since)
    print(json.dumps({"spend": s}) if a.json else s)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runq", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common_knobs(sp):
        sp.add_argument("--group", required=True)
        sp.add_argument("--name", required=True)
        sp.add_argument("--est-minutes", type=int, default=None,
                        help="explicit runtime estimate; if omitted, use the learned per-entrypoint "
                             "default (named add) or resources.est_minutes (manifest). "
                             "Explicit always overrides.")
        sp.add_argument("--slots", type=int, default=None,
                        help="lanes this task occupies; default = the manifest's "
                             "resources.slots, else 1. A job running K worker "
                             "processes must claim K slots.")
        sp.add_argument("--priority", type=int, default=None,
                        help=f"scheduling priority (default {DEFAULT_PRIORITY}; "
                             f"{PROBE_PRIORITY} with --probe). Explicit always wins.")
        sp.add_argument("--probe", action="store_true",
                        help=f"this is a QUICK TEST OF A HYPOTHESIS whose answer blocks a decision. "
                             f"Sets priority {PROBE_PRIORITY}, which clears the dispatcher's "
                             f"preempt margin over ordinary work — so it gracefully preempts a "
                             f"running lower-priority job (checkpoint -> requeue -> the victim "
                             f"relocates, renting a Vast box if the owned boxes are full) instead of "
                             f"queueing behind an 8-hour run. Rejected if est_minutes > "
                             f"{PROBE_MAX_MINUTES}: a long job is not a probe, and marking a sweep "
                             f"as one evicts real work for hours.")
        sp.add_argument("--max-retries", type=int, default=10)
        sp.add_argument("--by", default=None)
        sp.add_argument("--force", action="store_true")
        sp.add_argument("--vram-per-lane-gb", type=float, default=None)
        sp.add_argument("--cores-per-lane", type=int, default=None)
        sp.add_argument("--box", default=None, metavar="ID_OR_LABEL",
                        help="run ONLY on this box — an instance id (e.g. -1, 40000046) or an owned "
                             "box label (e.g. laptop-gpu). For reads that are about a MACHINE "
                             "rather than about the work: does this reproduce on a real GPU, what "
                             "does this box deliver saturated, reproduce the failure that only "
                             "happens on the laptop. The task will NEVER rent (no rental can become "
                             "the named box) and holds with `box_target: ...` until that box has "
                             "room; an unknown id/label holds forever and says so. Use "
                             "--cpu-name-include instead when you want a CPU MODEL rented for you.")
        sp.add_argument("--colocate", default=None, metavar="KEY",
                        help="run this task on the SAME box as every other task carrying this key "
                             "— the coordinator picks WHICH box (the first member to be placed "
                             "pins the group; the rest board it). This is what a PAIRED comparison "
                             "needs: arms measured on different boxes are not paired (the box "
                             "selects the attractor on a bistable rung), and a collapsed control "
                             "manufactures a win. Unlike --box it makes no placement decision and "
                             "does not serialise a campaign — give each SEED its own key and the "
                             "seeds still spread across the fleet. `runq sweep` sets this "
                             "automatically per seed; pass it by hand to add an arm to an existing "
                             "group. Mutually exclusive with --box. Verify with `runq colocate`.")
        sp.add_argument("--force-box", action="store_true",
                        help="⛔ OWNER-AUTHORIZED USE ONLY (working rules, 'Box pinning — only with owner "
                             "authorization'). Run this task on the --box you name AS SOON AS that "
                             "box is live, IGNORING its capacity gates: the time-of-day slot cap, "
                             "the window's cores/VRAM budget, measured headroom, the learned "
                             "over-pack cap and the slot ceiling — and the host's CPU / GPU-power "
                             "caps lift while it is on the box. Requires --box; the box must be an "
                             "OWNED box (it never rents); mutually exclusive with --colocate. It "
                             "does NOT override box state — a paused, draining, quarantined or "
                             "unreachable box still holds it — and it never evicts what is already "
                             "running there, so it can oversubscribe the machine. Every forced "
                             "placement is logged as a `forced_placement` event naming the gates "
                             "it bypassed (dispatcher inv. 4i).")
        sp.add_argument("--init-from", default=None,
                        help="home path to a checkpoint to initialize this task from (cross-task "
                             "handoff). Shipped to the box as resume.pt + the contract's resume "
                             "flag. Requires a declared resume flag (entrypoint or the config's `job` section).")
        sp.add_argument("--no-resume", default=None, metavar="REASON",
                        help="REMOVED — kept only to give a pointed error. Declare the opt-out in "
                             'the config\'s `job` section instead: "resume": {"none": "<reason>"}. '
                             "An opt-out passed on the command line does not survive the command, "
                             "so nothing downstream can tell it from a forgotten resume block.")

    add = sub.add_parser("add")
    _common_knobs(add)
    # Exactly one of these is required (validated in cmd_add): a named table entrypoint, or a
    # a trainer config carrying a self-describing `job` section (job-artifact-contract spec).
    add.add_argument("--entrypoint", default=None,
                     help="a named entrypoint from entrypoints.py (legacy path)")
    add.add_argument("--config", default=None, metavar="PATH",
                     help="the trainer config file, FULL path. Its reserved `job` section is the "
                          "run contract (git-optional; no entrypoints.py entry needed). Nothing is "
                          "inherited — a config with no `job` section is rejected.")

    submit = sub.add_parser("submit")
    _common_knobs(submit)
    submit.add_argument("artifact", metavar="CODE_TAR_GZ",
                        help="a pre-built gzipped code tar (git-archive layout). Nothing is run "
                             "locally; dedupe is content-addressed.")
    submit.add_argument("--config", default=None, metavar="PATH", required=True,
                        help="the config's path INSIDE the tar; its `job` section is the run "
                             "contract. No tar member is magic by filename.")

    ls = sub.add_parser("ls")
    ls.add_argument("--state", action="append")
    ls.add_argument("--group", default=None)
    ls.add_argument("--json", action="store_true")

    show = sub.add_parser("show")
    show.add_argument("task_id")
    show.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("task_id")
    # REQUIRED (owner directive 2026-07-31). A bare `cancelled` cannot be told apart from a
    # budget-complete stop, which silently corrupts every cost and completion read over this
    # registry — see registry_db.cancel_task. Free text, but say WHICH: "flops budget reached",
    # "superseded by <group>", "misconfigured", "scout read KILL".
    cancel.add_argument("--reason", required=True,
                        help="why this task is being cancelled (required; recorded in the event "
                             "log so budget-complete can be told apart from abandoned)")

    dupes = sub.add_parser("dupes")
    g = dupes.add_mutually_exclusive_group(required=True)
    g.add_argument("--of", default=None)
    g.add_argument("--hash", default=None)
    dupes.add_argument("--arm", action="store_true")
    dupes.add_argument("--json", action="store_true")

    rate = sub.add_parser("rate")
    rate.add_argument("--json", action="store_true")

    spend = sub.add_parser("spend")
    spend.add_argument("--since", default=None)
    spend.add_argument("--json", action="store_true")

    sweep = sub.add_parser("sweep", help="expand a sweep.json into N queued tasks "
                                         "(docs/specs/runq-sweep.spec.md)")
    sweep.add_argument("sweep_json", metavar="SWEEP_JSON")
    sweep.add_argument("--dry-run", action="store_true")
    sweep.add_argument("--group", default=None, help="override the sweep file's group")
    sweep.add_argument("--by", default=None)
    sweep.add_argument("--force", action="store_true")
    sweep.add_argument("--max-cells", type=int, default=64)
    sweep.add_argument("--est-minutes", type=int, default=None)
    sweep.add_argument("--priority", type=int, default=None)
    sweep.add_argument("--box", default=None, metavar="ID_OR_LABEL",
                       help="pin EVERY cell to this one box (see `add --box`). Rarely what you "
                            "want: it serialises the whole sweep behind one machine and makes a "
                            "placement decision for the coordinator. Co-location per paired seed "
                            "is automatic and is the right tool for arm-vs-arm comparability.")
    sweep.add_argument("--no-colocate", action="store_true",
                       help="do NOT auto-colocate this sweep's cells per seed. For a sweep whose "
                            "cells are not a paired comparison (an independent grid), where "
                            "forcing a seed's cells onto one box only costs parallelism.")
    sweep.add_argument("--colocate-by", action="append", default=None, metavar="PATH",
                       help="dotted axis path whose value identifies the paired seed (repeatable). "
                            "Default: auto-detect any axis whose last segment is seed/seeds; a "
                            "sweep with no seed axis is ONE group (its seed comes from the base "
                            "config, so every cell is the same paired seed).")

    box = sub.add_parser("box", help="ask the coordinator about a registered box")
    box_sub = box.add_subparsers(dest="box_cmd", required=True)
    box_probe = box_sub.add_parser("probe", help="check a box's reachability NOW, via the "
                                                 "coordinator's own keys")
    box_probe.add_argument("box", metavar="ID_OR_LABEL",
                           help="instance id (e.g. -2) or owned-box label (e.g. tower)")
    box_probe.add_argument("--wait", action="store_true",
                           help="block for the coordinator's answer and exit non-zero if the box is "
                                "unreachable, printing the ssh error")
    box_probe.add_argument("--timeout", type=int, default=PROBE_WAIT_S, metavar="S")
    box_key = box_sub.add_parser("key", help="which ssh identity the coordinator reaches this box "
                                             "with; unset = the fleet key")
    box_key.add_argument("box", metavar="ID_OR_LABEL")
    box_key.add_argument("name", nargs="?", default=None,
                         help="a key NAME in the coordinator's ~/.ssh (not a path). Omit to read it.")
    box_key.add_argument("--clear", action="store_true", help="fall back to the fleet key")

    colocate = sub.add_parser("colocate", help="report/verify sibling co-location groups "
                                               "(dispatcher inv. 4g)")
    colocate.add_argument("--group", default=None, help="only tasks in this task group")
    colocate.add_argument("--key", default=None, help="only this colocation key")
    colocate.add_argument("--json", action="store_true")
    colocate.add_argument("--verify", action="store_true",
                          help="exit 1 if any group SPLIT across boxes — a paired comparison that "
                               "did not actually pair. Use it before reporting a result.")
    colocate.add_argument("--release", default=None, metavar="KEY",
                          help="drop KEY's box pin, so its next member re-pins the group wherever "
                               "it lands. For a group wedged on a box that is never coming back.")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    own_args, entry_args = _split_on_dashdash(argv)
    parser = build_parser()
    a = parser.parse_args(own_args)
    if a.cmd in ("add", "submit", "sweep"):
        label, err = _resolve_actor(a)  # run-registry invariant 15: actor required
        if err is not None:
            return err
        a.by = label
    # A failed build blows the WHOLE task and hands it back to whoever asked for the dispatch
    # (ship-artifact-build spec inv. 13, owner decision 2026-07-31). There is no source fallback:
    # exit 5, queue nothing. For a sweep this is all-or-nothing by construction (inv. 14) — every
    # cell shares one code snapshot, so the first cell's build failure aborts before any insert.
    try:
        if a.cmd == "add":
            return cmd_add(a, entry_args)
        if a.cmd == "submit":
            return cmd_submit(a, entry_args)
        if entry_args:
            print(f"runq {a.cmd}: unexpected args after --", file=sys.stderr)
            return 2
        return {
            "ls": cmd_ls, "show": cmd_show, "cancel": cmd_cancel, "dupes": cmd_dupes,
            "rate": cmd_rate, "spend": cmd_spend, "sweep": cmd_sweep, "colocate": cmd_colocate,
            "box": cmd_box,
        }[a.cmd](a)
    except artifact_store.BuildFailed as e:
        print(f"runq {a.cmd}: BUILD FAILED — nothing was queued.\n  {e}", file=sys.stderr)
        if e.fatal_hint:
            print(f"  {e.fatal_hint}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
