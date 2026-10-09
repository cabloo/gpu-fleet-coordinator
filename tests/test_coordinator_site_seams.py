"""The coordinator can be used from OUTSIDE the project whose runs it stores (public-coordinator
spec, Part B, invariant B3).

Until now every path the coordinator needed was found from where its own files sit: the data root
through the repository around them, the capacity schedules, the learned estimates and the deny list
beside them. That is right while the coordinator lives in the project and wrong the moment a project
pins it from a separate checkout, where "the repository around these files" is the coordinator's
own. Two variables let the project say where its things are, and one flag lets a host keep the names
it was set up with.

Every test here has a CONTROL: the same question asked without the variable, because a seam that is
also taken when nobody asked for it would move a live fleet's registry.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VAST = ROOT / "fleet"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry_db", "fleet/registry_db.py")
disp = _load("dispatcher", "fleet/dispatcher.py")

SCHED = {"tz": "UTC", "cores": 20, "vram_gb": 8,
         "windows": [{"from": "00:00", "to": "24:00", "cpu": 1.0, "vram": 1.0}]}


def _fresh_import(env_extra: dict) -> dict:
    """What the module-level paths are in a NEW process (they are fixed at import)."""
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(VAST)!r})\n"
        "import registry_db, runq, dispatcher, est_defaults\n"
        "print(json.dumps({'root': str(registry_db.shared_experiments_root()),\n"
        "                  'runq_db': runq.DEFAULT_DB,\n"
        "                  'disp_root': str(dispatcher.EXPERIMENTS_ROOT),\n"
        "                  'lock': str(dispatcher.LOCK_PATH),\n"
        "                  'deny_seed': str(dispatcher.MACHINES_DENY),\n"
        "                  'deny_runtime': str(dispatcher.MACHINES_DENY_RUNTIME),\n"
        "                  'sidecar': str(est_defaults.SIDECAR)}))\n")
    env = {k: v for k, v in os.environ.items() if k not in ("FLEET_DATA_ROOT", "FLEET_SITE_DIR")}
    env.update(env_extra)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---- the data root ------------------------------------------------------------------------------

def test_a_named_data_root_is_used_by_every_consumer(tmp_path):
    named = tmp_path / "elsewhere" / "experiments"
    got = _fresh_import({"FLEET_DATA_ROOT": str(named)})
    assert got["root"] == str(named)
    assert got["runq_db"] == str(named / "runs.sqlite")
    assert got["disp_root"] == str(named)
    assert got["lock"] == str(named / ".dispatcher.lock")
    assert got["deny_runtime"] == str(named / ".dispatcher" / "machines.deny")


def test_CONTROL_without_the_variable_the_root_is_found_as_before():
    got = _fresh_import({})
    assert Path(got["root"]).name == "experiments"
    assert "elsewhere" not in got["root"]
    # the registry and the dispatcher still agree with each other, which is the property that matters
    assert got["runq_db"] == str(Path(got["root"]) / "runs.sqlite")
    assert got["disp_root"] == got["root"]


def test_an_empty_variable_is_not_a_root(monkeypatch):
    """`FLEET_DATA_ROOT=` (set, empty) must not resolve to the current directory."""
    monkeypatch.setenv("FLEET_DATA_ROOT", "   ")
    assert reg.shared_experiments_root().name == "experiments"
    assert reg.shared_experiments_root().is_absolute()


# ---- the site directory ---------------------------------------------------------------------------

def test_site_file_reads_the_variable_at_call_time(tmp_path, monkeypatch):
    default = Path("/somewhere/default.json")
    monkeypatch.delenv("FLEET_SITE_DIR", raising=False)
    assert reg.site_file("x.json", default) == default
    monkeypatch.setenv("FLEET_SITE_DIR", str(tmp_path))
    assert reg.site_file("x.json", default) == tmp_path / "x.json"
    assert reg.site_file("capacity/box.json", default) == tmp_path / "capacity" / "box.json"
    monkeypatch.setenv("FLEET_SITE_DIR", "")
    assert reg.site_file("x.json", default) == default


def test_the_deny_seed_and_the_learned_estimates_come_from_the_site_directory(tmp_path):
    got = _fresh_import({"FLEET_SITE_DIR": str(tmp_path)})
    assert got["deny_seed"] == str(tmp_path / "machines.deny")
    assert got["sidecar"] == str(tmp_path / "est_defaults.json")


def test_CONTROL_without_a_site_directory_both_stay_beside_the_code():
    got = _fresh_import({})
    assert got["deny_seed"] == str(VAST / "machines.deny")
    assert got["sidecar"] == str(VAST / "est_defaults.json")


class _Quiet:
    """A `run` that answers every command with success and no output."""

    def __call__(self, cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_a_capacity_schedule_is_read_from_the_site_directory(tmp_path, monkeypatch):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Quiet(), vastai_run=_Quiet())
    box = {"id": -9, "source": "owned", "label": "seam-test-box"}
    site = tmp_path / "site"
    (site / "capacity").mkdir(parents=True)
    (site / "capacity" / "seam-test-box.json").write_text(json.dumps(SCHED))

    monkeypatch.delenv("FLEET_SITE_DIR", raising=False)
    assert d._capacity_schedule(box) is None, "CONTROL: no such schedule beside the code"

    monkeypatch.setenv("FLEET_SITE_DIR", str(site))
    sched = d._capacity_schedule(box)
    assert sched is not None and sched["cores"] == 20
    assert d._capacity_schedule({**box, "label": "another-box"}) is None
    assert d._capacity_schedule({**box, "source": "vast"}) is None, "a rental never has a schedule"


# ---- the host's name ------------------------------------------------------------------------------

SETUP = VAST / "owned_box_setup.sh"


def test_the_setup_script_names_everything_after_one_value():
    """A host set up under one name and re-run under another gets a second installation beside the
    first, so the name must reach the image, the three directories, the container and the timer —
    and nothing may still spell a name out."""
    text = SETUP.read_text()
    body = text.split("NAME=", 1)[1]
    for derived in ("IMAGE=$NAME-owned-worker", "ETC=/etc/$NAME-worker",
                    "CONTROL=/var/lib/$NAME-worker/control", "OPT=/opt/$NAME-worker",
                    "UNIT=$NAME-capacity", 'WORKER="$NAME-${LABEL}-worker"'):
        assert derived in body, derived
    default = body.splitlines()[0].strip()
    spelled_out = [ln for ln in body.splitlines()[1:]
                   if any(f"{default}-{part}" in ln for part in ("worker", "owned-worker", "capacity"))
                   and "FLEET_PUBKEY" not in ln and not ln.lstrip().startswith("#")]
    assert not spelled_out, spelled_out


def test_the_setup_script_refuses_a_name_it_cannot_use_in_a_path():
    out = subprocess.run(["bash", str(SETUP), "--name", "Not A Name", "--label", "x"],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "--name must be" in out.stderr


@pytest.mark.parametrize("flag", ["--name"])
def test_the_flag_is_in_the_help(flag):
    out = subprocess.run(["bash", str(SETUP), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and flag in out.stdout
    assert "set -euo pipefail" not in out.stdout, "the help prints past the header"
