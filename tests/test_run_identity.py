"""Run identity — golden hashes from docs/specs/run-registry.spec.md."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from run_identity import arm_hash, canonical_config, config_hash, print_run_identity

FIX = Path(__file__).resolve().parent / "fixtures" / "registry"


def _fixture():
    return json.loads((FIX / "canonical.json").read_text())


def test_canonical_strips_and_serializes():
    fx = _fixture()
    exclude = frozenset(fx["exclude"])
    assert canonical_config(fx["cfg"], exclude) == fx["canonical"]


def test_config_hash_matches_golden():
    fx = _fixture()
    assert config_hash(fx["cfg"]) == fx["config_hash"]


def test_arm_hash_matches_golden():
    fx = _fixture()
    assert arm_hash(fx["cfg"]) == fx["arm_hash"]


def test_key_order_never_affects_hash():
    fx = _fixture()
    reordered = {
        "world_model": {"lr": 0.0003, "deter": 256},
        "train_ratio": 1,
        "run": {"name": "tr1", "out_dir": "/tmp/x"},
        "seed": 3,
    }
    assert config_hash(reordered) == fx["config_hash"]
    assert arm_hash(reordered) == fx["arm_hash"]


def test_run_dropped_entirely_once_emptied():
    canon = canonical_config({"a": 1, "run": {"out_dir": "/x"}}, frozenset({"run.out_dir"}))
    assert "run" not in json.loads(canon)


def _captured_identity(cfg, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_identity(cfg, **kw)
    return json.loads(buf.getvalue())


class TestPrintRunIdentity:
    def test_moves_flat_out_tag_device_seed_under_run(self):
        payload = _captured_identity(
            {"lr": 0.001, "out": "/tmp/x", "tag": "t1", "device": "cuda", "seed": 3},
            seed=3, out_dir="/tmp/x", tag="t1", device="cuda")
        assert payload == {"config": {"lr": 0.001, "seed": 3,
                                       "run": {"out_dir": "/tmp/x", "tag": "t1", "device": "cuda"}}}

    def test_works_when_cfg_never_had_those_fields(self):
        """train_grid/train_chess-style: out/tag/seed live outside cfg entirely."""
        payload = _captured_identity({"lr": 0.001}, seed=0, out_dir="/tmp/x", tag="smoke")
        assert payload == {"config": {"lr": 0.001, "seed": 0,
                                       "run": {"out_dir": "/tmp/x", "tag": "smoke"}}}

    def test_device_and_tag_are_optional(self):
        payload = _captured_identity({"lr": 0.001}, seed=0, out_dir="/tmp/x")
        assert payload == {"config": {"lr": 0.001, "seed": 0, "run": {"out_dir": "/tmp/x"}}}

    def test_hashable_and_excludable_by_run_identity_conventions(self):
        payload = _captured_identity(
            {"lr": 0.001, "out": "/tmp/a"}, seed=1, out_dir="/tmp/a", tag="a")["config"]
        payload_b = _captured_identity(
            {"lr": 0.001, "out": "/tmp/b"}, seed=1, out_dir="/tmp/b", tag="b")["config"]
        assert config_hash(payload) == config_hash(payload_b)  # out/tag don't affect the hash
        payload_seed2 = _captured_identity(
            {"lr": 0.001, "out": "/tmp/a"}, seed=2, out_dir="/tmp/a", tag="a")["config"]
        assert arm_hash(payload) == arm_hash(payload_seed2)  # same arm despite differing seed
        assert config_hash(payload) != config_hash(payload_seed2)  # different exact run though
