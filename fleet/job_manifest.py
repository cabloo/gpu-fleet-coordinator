"""Job manifest — the self-describing run contract that travels INSIDE the trainer config.

Spec: `docs/specs/job-artifact-contract.spec.md` (v2, 2026-07-27). A reserved `job` section of the
trainer config declares how to run a job (its `run` argv prefix, what proves completion, whether/how
it resumes, what deps it needs) so the coordinator no longer needs the job's contract hardcoded in
`entrypoints.py`. The coordinator reads this off the config once at submission, validates it, and
stores the resolved contract on the task row; every later stage reads the row, never the tar.

**v1 kept this in a loose `job.json` at the code-tree root and that was the bug.** One untracked,
mutable file supplied the config to every `runq add --job .` and every sweep cell — so a sweep file
could queue cells for a config it never names, which happened. v2 has NO magic filename anywhere:
the caller names the config in full (`--config path/to/x.json`) and the run contract is a section
of that same file, so there is exactly one path to disagree about and nothing to inherit.

Home-side only (stdlib-only, never imported on the box). `entrypoints.py` imports THIS lazily for
its `resolve()` seam; `to_entrypoint` imports `entrypoints` lazily — neither imports the other at
module load, so there is no import cycle.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

JOB_MANIFEST_VERSION = 1
JOB_SECTION = "job"          # the reserved key in a trainer config; there is no manifest FILENAME
DEFAULT_RESUME_CHECKPOINT = "ckpt_latest.pt"


class JobManifestError(Exception):
    """A `job` section failed validation at the submission trust boundary."""


@dataclass(frozen=True)
class JobManifest:
    run: list                       # argv prefix; submission args are appended after it
    completion_artifact: str        # path under the run's out dir; existence gates `done`
    resume_flag: str | None = None  # e.g. "--init-from"; None ⇒ no resume support
    resume_checkpoint: str = DEFAULT_RESUME_CHECKPOINT
    pip: list = field(default_factory=list)   # setup.pip — installed on the box before first run
    apt: list = field(default_factory=list)   # setup.apt — apt-installed over ssh at ship time
    resources: dict = field(default_factory=dict)  # {slots?, vram_gb?, cores?, est_minutes?, requires_gpu?, box?, force_box?}
    #: `resume: {"none": "<reason>"}` — a DECLARED, durable opt-out for a job that genuinely cannot
    #: resume. Lives on the artifact, not on the queue-time invocation, so `check_submittable` and
    #: any auditor read the same bytes. See `check_submittable` for why that matters.
    no_resume_reason: str | None = None


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise JobManifestError(msg)


def _is_str_list(x) -> bool:
    return isinstance(x, list) and all(isinstance(e, str) for e in x)


def parse(data: dict) -> JobManifest:
    """Validate a raw `job` section against the schema (spec invariant 2). Fail-closed: any
    missing/wrong field raises JobManifestError, and the caller aborts the submission."""
    _require(isinstance(data, dict), "the `job` section must be a JSON object")
    ver = data.get("manifest_version")
    _require(ver == JOB_MANIFEST_VERSION,
             f"manifest_version must be {JOB_MANIFEST_VERSION}, got {ver!r}")

    run = data.get("run")
    _require(_is_str_list(run) and len(run) > 0, "run must be a non-empty list of strings")
    # `runq` appends `--config <the path the caller named>`; a `run` that also carries one could
    # disagree with the file the block lives in, which is the exact silent-wrong-config failure v2
    # exists to remove. Reject rather than pick a winner.
    _require("--config" not in run,
             "run must not contain '--config' — runq appends the config path you name at the call "
             "site (job-artifact-contract spec v2). Drop it from `run`.")

    ca = data.get("completion_artifact")
    _require(isinstance(ca, str) and ca != "", "completion_artifact must be a non-empty string")
    _require(not PurePosixPath(ca).is_absolute() and ".." not in PurePosixPath(ca).parts,
             f"completion_artifact must be a relative path without '..': {ca!r}")

    resume_flag = None
    resume_ckpt = DEFAULT_RESUME_CHECKPOINT
    no_resume_reason = None
    resume = data.get("resume")
    if resume is not None:
        _require(isinstance(resume, dict), "resume must be an object")
        # STRUCTURE only — never the est_minutes policy. `parse` is also how `entrypoints.resolve`
        # re-reads the manifest STORED on an already-queued task row, so a policy rejection here
        # would make live tasks unresolvable at dispatch. Policy lives in `check_submittable`.
        if "none" in resume:
            _require("flag" not in resume,
                     "resume declares both `flag` and `none` — a job either resumes or documents "
                     "why it cannot, never both")
            reason = resume["none"]
            _require(isinstance(reason, str) and reason.strip() != "",
                     "resume.none must be a non-empty reason string — an undocumented opt-out is "
                     "indistinguishable from having forgotten the resume block, which is the whole "
                     "failure this field exists to make auditable")
            no_resume_reason = reason
        else:
            resume_flag = resume.get("flag")
            _require(isinstance(resume_flag, str) and resume_flag != "",
                     "resume.flag must be a non-empty string")
            if "checkpoint" in resume:
                _require(isinstance(resume["checkpoint"], str) and resume["checkpoint"] != "",
                         "resume.checkpoint must be a non-empty string")
                resume_ckpt = resume["checkpoint"]

    setup = data.get("setup") or {}
    _require(isinstance(setup, dict), "setup must be an object")
    pip = setup.get("pip", [])
    apt = setup.get("apt", [])
    _require(_is_str_list(pip), "setup.pip must be a list of strings")
    _require(_is_str_list(apt), "setup.apt must be a list of strings")

    resources = data.get("resources") or {}
    _require(isinstance(resources, dict), "resources must be an object")
    for k in ("slots", "vram_gb", "cores", "est_minutes"):
        if k in resources:
            _require(isinstance(resources[k], (int, float)) and not isinstance(resources[k], bool),
                     f"resources.{k} must be a number")
    # ⛔ `requires_gpu` (invariant 4h) IS A BOOL AND MUST BE VALIDATED SEPARATELY. Adding it to the
    # numeric loop above was exactly backwards — that loop EXCLUDES bools on purpose
    # (`not isinstance(..., bool)`, so a stray `True` cannot pass as a lane count), so every config
    # that set it was rejected with "resources.requires_gpu must be a number" and four queued cells
    # bounced. ⚠ The first version of this shipped to master because its test only grepped the file
    # for the string "requires_gpu" instead of PARSING a manifest that sets it — a check that could
    # not fail for the reason it named.
    if "requires_gpu" in resources:
        _require(isinstance(resources["requires_gpu"], bool),
                 "resources.requires_gpu must be a boolean (true/false)")
    # `force_box` (dispatcher invariant 4i) is a bool for the same reason, and STRUCTURE is all that
    # is checked here: `parse` re-runs at dispatch, so the policy half (it needs a `box`, the box
    # must be owned, never with `colocate`) lives at the submission boundary —
    # `registry_db.force_box_error` — where refusing cannot strand an already-queued task.
    if "force_box" in resources:
        _require(isinstance(resources["force_box"], bool),
                 "resources.force_box must be a boolean (true/false)")

    return JobManifest(run=list(run), completion_artifact=ca, resume_flag=resume_flag,
                       resume_checkpoint=resume_ckpt, pip=list(pip), apt=list(apt),
                       resources=dict(resources), no_resume_reason=no_resume_reason)


#: The checkpoint+resume rule's threshold (owner directive 2026-07-14): "all tasks that take longer
#: than 10 minutes should have checkpoints and resume."
RESUME_REQUIRED_OVER_MINUTES = 10


def check_submittable(m: JobManifest, est_minutes: float | None = None) -> str | None:
    """POLICY gate for the submission trust boundary. Returns an error message, or None to allow.

    Separate from `parse` ON PURPOSE, and the split is load-bearing in both directions:

    * `parse` is STRUCTURE, and runs again at DISPATCH — `entrypoints.resolve` re-parses the
      manifest stored on the task row. Putting this rule there would make every task queued before
      the rule existed unresolvable, i.e. a schema change would strand live work.
    * this is POLICY, and runs only where money is about to be spent. It is a pure function of the
      manifest plus the submission's effective `est_minutes`, so the queue-time gate and any
      auditor reach the same verdict from the same bytes.

    That last property is the point. The opt-out used to be `runq add --no-resume '<reason>'` — a
    CLI flag checked for non-emptiness and then DISCARDED, never persisted anywhere. So a config
    committed with no resume block was indistinguishable from one deliberately opted out, and the
    repo-wide audit could not agree with the queue-time gate even in principle. Two configs
    (`pc_colorbook_bprior_{bias,ctrl}`) carried a hand-rolled `_resume_note` key read by NOTHING,
    because their author correctly wanted the justification to be durable and had nowhere to put
    it. Now they do: `resume: {"none": "<reason>"}`."""
    est = (m.resources.get("est_minutes") or 0) if est_minutes is None else est_minutes
    if m.resume_flag is not None or est <= RESUME_REQUIRED_OVER_MINUTES:
        return None
    if m.no_resume_reason:
        return None
    return (f"the config's `job` section declares no `resume` block, but est_minutes={est} exceeds "
            f"the {RESUME_REQUIRED_OVER_MINUTES}-minute checkpoint+resume rule. A preemption would "
            f"restart it from scratch. Add\n"
            f'      "resume": {{"flag": "--init-from", "checkpoint": "ckpt_latest.pt"}}\n'
            f"    to the manifest (the trainer must actually honour the flag — a driver that "
            f"accepts and ignores --init-from is the failure this check exists to catch).\n"
            f"    If the job genuinely CANNOT resume, declare that instead:\n"
            f'      "resume": {{"none": "<why a restart-from-scratch is acceptable here>"}}')


def to_dict(m: JobManifest) -> dict:
    """Canonical manifest dict (round-trips through `parse`). What runq stores in
    `tasks.job_manifest_json`, so `entrypoints.resolve` can re-parse it verbatim."""
    d: dict = {
        "manifest_version": JOB_MANIFEST_VERSION,
        "run": list(m.run),
        "completion_artifact": m.completion_artifact,
    }
    if m.resume_flag is not None:
        d["resume"] = {"flag": m.resume_flag, "checkpoint": m.resume_checkpoint}
    elif m.no_resume_reason is not None:
        # The declared opt-out rides along onto the task row, so the reason survives to whoever
        # later asks why this run had nothing to restart from.
        d["resume"] = {"none": m.no_resume_reason}
    if m.pip or m.apt:
        d["setup"] = {"pip": list(m.pip), "apt": list(m.apt)}
    if m.resources:
        d["resources"] = dict(m.resources)
    return d


def to_entrypoint(m: JobManifest):
    """Adapt a JobManifest to the existing `entrypoints.Entrypoint` so every downstream consumer
    (dispatcher ship/complete/compile, runq handshake) works through ONE code path."""
    import entrypoints  # lazy: breaks the entrypoints<->job_manifest import cycle
    return entrypoints.Entrypoint(
        argv=list(m.run),
        completion_artifact=m.completion_artifact,
        resume_flag=m.resume_flag,
        live=True,
        pip_extras=list(m.pip),
        apt_packages=list(m.apt),
    )


# --------------------------------------------------------------------------- readers

def _job_section(cfg: dict, where: str) -> dict | None:
    """The reserved `job` block out of a parsed trainer config. None ⇒ the config declares no run
    contract, which is a REJECT at the submission boundary (v2 has no inferred fallback)."""
    _require(isinstance(cfg, dict), f"{where}: top level must be a JSON object")
    job = cfg.get(JOB_SECTION)
    if job is None:
        return None
    _require(isinstance(job, dict), f"{where}: '{JOB_SECTION}' must be an object")
    return job


def read_tar(code_tar: bytes, config_path: str) -> dict | None:
    """Raw `job` block from a config INSIDE a gzipped code tar, read by a STREAMING open that stops
    at the named member (so this pulls ~O(config bytes) even for a multi-MB tree). `config_path` is
    the tar-relative path the caller named — v2 has no magic filename, so the member is located by
    the name the caller gave, not by convention. Returns None if the config carries no `job`
    section; raises if the member is absent or unparseable. Never extracts the tree to disk."""
    want = str(PurePosixPath(config_path))
    try:
        with tarfile.open(fileobj=io.BytesIO(code_tar), mode="r|gz") as tf:  # r|gz = seq. stream
            for member in tf:
                if member.name == want and member.isfile():
                    raw = tf.extractfile(member).read()
                    try:
                        cfg = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        raise JobManifestError(f"{want} is not valid UTF-8 JSON: {e}") from None
                    return _job_section(cfg, want)
    except tarfile.TarError as e:
        raise JobManifestError(f"artifact is not a readable gzip tar: {e}") from None
    raise JobManifestError(f"no member {want!r} in the artifact — --config must name the config's "
                           f"path INSIDE the tar")


def read_config(path: str | Path) -> dict | None:
    """Raw `job` block from a trainer config FILE on disk (the `runq add --config` path). None if
    the config declares no `job` section. Raises if the file is missing or not valid JSON."""
    p = Path(path)
    if not p.is_file():
        raise JobManifestError(f"config not found: {p}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JobManifestError(f"{p} is not valid UTF-8 JSON: {e}") from None
    return _job_section(cfg, str(p))


def from_tar(code_tar: bytes, config_path: str) -> JobManifest | None:
    raw = read_tar(code_tar, config_path)
    return parse(raw) if raw is not None else None


def from_config(path: str | Path) -> JobManifest | None:
    raw = read_config(path)
    return parse(raw) if raw is not None else None
