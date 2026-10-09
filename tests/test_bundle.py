"""Task bundle — build/verify/unpack + signing + compile plumbing (docs/specs/task-bundle.spec.md).

Fixtures here ARE the spec's acceptance cases (its "Fixtures" section). The real Cython-in-Docker
compile and a compiled trainer training end-to-end are only exercised by a paid box smoke (spec
"Verification status"); `compile_tree` is unit-tested at the plumbing level with a stubbed compiler.
"""

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fleet"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load("bundle", "fleet/bundle.py")
entrypoints = _load("entrypoints", "fleet/entrypoints.py")

TASK = {"task_id": "t1", "grp": "g", "name": "t1", "argv": ["python", "-m", "native.training.m36"],
        "env": {}, "est_minutes": 1, "git_sha": "abc", "pip_extras": [], "resume_from": None}


def _code_tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _repack(members: dict, dest: Path) -> Path:
    with tarfile.open(dest, "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return dest


def _keypair(tmp_path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    priv = tmp_path / "priv.pem"
    pub = tmp_path / "pub.pem"
    priv.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pub.write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub


class TestRoundTrip:
    def test_roundtrip_with_resume(self, tmp_path):
        code = _code_tar({"src/native/models/agent.py": b"x = 1\n", "README": b"r"})
        b = B.build_bundle(tmp_path / "b.tar", code_tar=code, task_json=TASK,
                           git_sha="abc", task_id="t1", resume=b"CKPT")
        got = B.unpack_bundle(b, tmp_path / "out")
        assert got == TASK
        assert (tmp_path / "out" / "repo" / "src" / "native" / "models" / "agent.py").read_bytes() == b"x = 1\n"
        assert (tmp_path / "out" / "resume.pt").read_bytes() == b"CKPT"
        assert (tmp_path / "out" / "task.json").exists()

    def test_manifest_records_format_and_version(self, tmp_path):
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1", code_format="compiled")
        m = B.read_manifest(b)
        assert m["bundle_version"] == B.BUNDLE_VERSION
        assert m["code_format"] == "compiled"
        assert "resume.pt" not in m["members"]


class TestIntegrity:
    def test_corrupt_member_rejected_and_nothing_extracted(self, tmp_path):
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1")
        members = B._read_all_members(b)
        members["code.tar.gz"] = members["code.tar.gz"][:-1] + bytes([members["code.tar.gz"][-1] ^ 0xFF])
        bad = _repack(members, tmp_path / "bad.tar")
        with pytest.raises(B.BundleError, match="integrity"):
            B.unpack_bundle(bad, tmp_path / "out")
        assert not (tmp_path / "out" / "repo").exists()

    def test_member_set_mismatch_rejected(self, tmp_path):
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1")
        members = B._read_all_members(b)
        members["stowaway"] = b"undeclared"  # in tar, not in manifest
        bad = _repack(members, tmp_path / "bad.tar")
        with pytest.raises(B.BundleError, match="member set mismatch"):
            B.verify_bundle(bad)

    def test_bad_version_rejected(self, tmp_path):
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1")
        members = B._read_all_members(b)
        m = json.loads(members["manifest.json"])
        m["bundle_version"] = 999
        members["manifest.json"] = json.dumps(m).encode()
        bad = _repack(members, tmp_path / "bad.tar")
        with pytest.raises(B.BundleError, match="unsupported bundle version"):
            B.verify_bundle(bad)

    def test_path_traversal_rejected(self, tmp_path):
        evil = _code_tar({"../evil.py": b"bad"})
        b = B.build_bundle(tmp_path / "b.tar", code_tar=evil, task_json=TASK,
                           git_sha="abc", task_id="t1")
        with pytest.raises(B.BundleError, match="unsafe path"):
            B.unpack_bundle(b, tmp_path / "out")
        assert not (tmp_path / "out" / "repo" / "evil.py").exists()

    def test_validate_hook_rejects_before_extract(self, tmp_path):
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1")
        with pytest.raises(B.BundleError, match="task.json rejected"):
            B.unpack_bundle(b, tmp_path / "out", validate=lambda t: "nope")
        assert not (tmp_path / "out" / "repo").exists()


@pytest.mark.skipif(not B.sign_available(), reason="cryptography not installed")
class TestSigning:
    def test_signed_bundle_verifies(self, tmp_path):
        priv, pub = _keypair(tmp_path)
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1", sign_key=priv)
        B.unpack_bundle(b, tmp_path / "out", public_key=pub, require_signature=True)  # no raise

    def test_unsigned_rejected_when_signature_required(self, tmp_path):
        _priv, pub = _keypair(tmp_path)
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1")  # unsigned
        with pytest.raises(B.BundleError, match="signature"):
            B.unpack_bundle(b, tmp_path / "out", public_key=pub, require_signature=True)

    def test_wrong_key_rejected(self, tmp_path):
        priv, _pub = _keypair(tmp_path)                       # signer
        _priv2, pub2 = _keypair(tmp_path / "verifier")        # a genuinely different keypair
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1", sign_key=priv)
        with pytest.raises(B.BundleError, match="signature"):
            B.unpack_bundle(b, tmp_path / "out", public_key=pub2, require_signature=True)

    def test_no_pubkey_ignores_signature(self, tmp_path):
        priv, _pub = _keypair(tmp_path)
        b = B.build_bundle(tmp_path / "b.tar", code_tar=_code_tar({"a": b"a"}), task_json=TASK,
                           git_sha="abc", task_id="t1", sign_key=priv)
        B.unpack_bundle(b, tmp_path / "out")  # integrity-only, no raise


class TestEntrySourcePath:
    def test_dash_m_module(self):
        # a synthetic `-m` entrypoint (legacy trainer rows were removed with the 2026-07-19
        # tombstoning; the resolution rule itself is what this covers)
        e = entrypoints.Entrypoint(argv=["python", "-m", "native.training.m36"],
                                   completion_artifact="results.json")
        assert entrypoints.entry_source_path(e) == "src/native/training/m36.py"

    def test_script_form(self):
        e = entrypoints.get("smoke")
        assert entrypoints.entry_source_path(e) == "fleet/smoke_entrypoint.py"


class TestOverlayEntrySource:
    def test_swaps_entry_so_for_py_keeps_rest(self):
        # a compile-everything tree: agent + the entry both compiled to .so, __init__ kept source
        compiled = _code_tar({
            "src/native/models/agent.cpython-312-x86_64-linux-gnu.so": b"AGENT_SO",
            "src/native/training/lm.cpython-312-x86_64-linux-gnu.so": b"ENTRY_SO",
            "src/native/__init__.py": b"",
        })
        source = _code_tar({"src/native/training/lm.py": b"def main():\n    pass\n",
                            "src/native/models/agent.py": b"x=1\n"})
        out = B.overlay_entry_source(compiled, "src/native/training/lm.py", source)
        got = {}
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    got[m.name.lstrip("./")] = tf.extractfile(m).read()
        assert "src/native/training/lm.py" in got                             # entry restored to source
        assert got["src/native/training/lm.py"] == b"def main():\n    pass\n"
        assert "src/native/training/lm.cpython-312-x86_64-linux-gnu.so" not in got   # entry .so dropped
        assert got["src/native/models/agent.cpython-312-x86_64-linux-gnu.so"] == b"AGENT_SO"  # others kept
        assert "src/native/__init__.py" in got

    def test_missing_entry_source_raises(self):
        compiled = _code_tar({"src/native/models/agent.cpython-312-x86_64-linux-gnu.so": b"A"})
        with pytest.raises(B.BundleError, match="not found in the source tree"):
            B.overlay_entry_source(compiled, "src/native/training/lm.py", _code_tar({"other.py": b"x"}))

    def test_uncompiled_entry_is_noop_reaffirm(self):
        # smoke's entry lives outside the compiled packages -> already source in the tree; overlay
        # just re-affirms it (no .so to drop).
        compiled = _code_tar({"fleet/smoke_entrypoint.py": b"print(1)\n",
                              "src/native/models/agent.cpython-312-x86_64-linux-gnu.so": b"A"})
        source = _code_tar({"fleet/smoke_entrypoint.py": b"print(1)\n"})
        out = B.overlay_entry_source(compiled, "fleet/smoke_entrypoint.py", source)
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as tf:
            names = {m.name.lstrip("./") for m in tf.getmembers() if m.isfile()}
        assert "fleet/smoke_entrypoint.py" in names
        assert "src/native/models/agent.cpython-312-x86_64-linux-gnu.so" in names


def _abi() -> str:
    import sysconfig
    return sysconfig.get_config_var("EXT_SUFFIX")


class TestCompileTree:
    def test_docker_backend_missing_docker_raises(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        with pytest.raises(B.BundleError, match="docker unavailable"):
            B.compile_tree(_code_tar({"src/native/models/agent.py": b"x=1\n"}), packages=["src/native"],
                           keep_source=[], image="img", backend="docker")

    def test_unknown_backend_raises(self, tmp_path):
        with pytest.raises(B.BundleError, match="unknown compile backend"):
            B.compile_tree(_code_tar({"a": b"a"}), packages=[], keep_source=[], image="img",
                           backend="nope")

    def test_local_abi_mismatch_fails_loud(self, tmp_path):
        # a build interpreter whose EXT_SUFFIX doesn't match the box ABI must be refused, not ship
        # an unloadable .so (spec inv. 6 / the local-backend guard).
        def stub(cmd, **kwargs):
            class R:
                returncode, stdout, stderr = 0, "cpython-311-x86_64-linux-gnu\n", ""
            return R()
        with pytest.raises(B.BundleError, match="ABI mismatch") as ei:
            B.compile_tree(_code_tar({"src/native/models/agent.py": b"x=1\n"}), packages=["src/native"],
                           keep_source=[], image="img", backend="local", python_bin="/fake/py",
                           expected_abi="cpython-312-x86_64-linux-gnu", run=stub)
        # An ABI/toolchain LIMIT is a plain BundleError, NOT a CompileFailed — so the dispatcher
        # falls back to source (keeps the fleet running) rather than failing the task fast (inv. 6).
        assert not isinstance(ei.value, B.CompileFailed)

    def test_local_plumbing_with_stub_compiler(self, tmp_path):
        """compile_tree (local): ABI-probe, run the driver, re-tar the tree. A stub stands in for
        the driver — swapping non-kept .py for .so, keeping keep_source .py (spec inv. 7)."""
        def stub(cmd, **kwargs):
            if "-c" in cmd:                      # the EXT_SUFFIX ABI probe
                class R:
                    returncode, stdout, stderr = 0, "cpython-311-x86_64-linux-gnu\n", ""
                return R()
            repo = Path(cmd[2])                  # driver argv: <driver> <repo> <pkgs> <keep>
            packages = cmd[3].split(",") if cmd[3] else []
            keep = {k for k in cmd[4].split(",") if k}
            for pkg in packages:
                for py in list((repo / pkg).rglob("*.py")):
                    if py.relative_to(repo).as_posix() in keep:
                        continue
                    py.with_suffix(".cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
                    py.unlink()
            class R:
                returncode, stdout, stderr = 0, "compiled 2 modules", ""
            return R()

        code = _code_tar({"src/native/models/agent.py": b"x=1\n", "src/native/training/atari.py": b"# entry\n",
                          "src/shared/util.py": b"y=2\n"})
        out_tar = B.compile_tree(code, packages=["src/native", "src/shared"],
                                 keep_source=["src/native/training/atari.py"], image="img",
                                 backend="local", python_bin="/fake/py",
                                 expected_abi="cpython-311", run=stub, work_root=tmp_path)
        with tarfile.open(fileobj=io.BytesIO(out_tar), mode="r:gz") as tf:
            names = {m.name.lstrip("./") for m in tf.getmembers() if m.isfile()}
        assert "src/native/models/agent.cpython-311-x86_64-linux-gnu.so" in names
        assert "src/native/models/agent.py" not in names          # compiled away
        assert "src/native/training/atari.py" in names        # entry kept as source (invariant 7)
        assert "src/shared/util.cpython-311-x86_64-linux-gnu.so" in names

    def test_compile_failure_raises(self, tmp_path):
        # A driver rc!=0 (the compiler REJECTED the code) is a CompileFailed, not a plain BundleError
        # — so the dispatcher fails the task fast rather than shipping source (spec inv. 6).
        def stub(cmd, **kwargs):
            if "-c" in cmd:
                class R:
                    returncode, stdout, stderr = 0, _abi() + "\n", ""
                return R()
            class R2:
                returncode, stdout, stderr = 1, "", "cython exploded"
            return R2()
        with pytest.raises(B.CompileFailed, match="compile_tree failed"):
            B.compile_tree(_code_tar({"src/native/models/agent.py": b"x=1\n"}), packages=["src/native"],
                           keep_source=[], image="img", backend="local", python_bin="/fake/py",
                           expected_abi=_abi(), run=stub, work_root=tmp_path)

    def test_the_failure_message_carries_the_DIAGNOSTIC_not_the_traceback_tail(self, tmp_path):
        """A rejected compile must say WHAT was wrong, not just which file.

        Regression pin for a real queue-blocking incident (2026-08-04). `cythonize(nthreads=4)`
        re-raises in the parent, so the LAST bytes of stderr are always `concurrent.futures`
        boilerplate; reporting `stderr[-500:]` named the failing file and withheld the one line that
        said why. Measured on the real failure: 3619 bytes of stderr, diagnostic on line 13, and the
        reported tail was pure `_base.py` frames — the error had to be reproduced by re-running the
        compiler serially by hand to read it."""
        real_shaped_stderr = (
            "Error compiling Cython file:\n"
            "------------------------------------------------------------\n"
            "        dr, dc = move_of(_MOVES, action)\n"
            "                 ^\n"
            "------------------------------------------------------------\n"
            "\n"
            "src/native/evo/world.py:72:17: undeclared name not builtin: move_of\n"
            + "Traceback (most recent call last):\n"
            + "".join(f'  File "/x/concurrent/futures/_base.py", line {i}, in result\n'
                      "    return self.__get_result()\n" for i in range(40))
            + "Cython.Compiler.Errors.CompileError: src/native/evo/world.py\n")
        assert len(real_shaped_stderr[-500:]) == 500, "fixture must be long enough to truncate"
        assert "undeclared name" not in real_shaped_stderr[-500:], \
            "fixture must reproduce the bug: the diagnostic is OUTSIDE the reported tail"

        def stub(cmd, **kwargs):
            if "-c" in cmd:
                class R:
                    returncode, stdout, stderr = 0, _abi() + "\n", ""
                return R()
            class R2:
                returncode, stdout, stderr = 1, "", real_shaped_stderr
            return R2()
        with pytest.raises(B.CompileFailed) as ei:
            B.compile_tree(_code_tar({"src/native/models/agent.py": b"x=1\n"}), packages=["src/native"],
                           keep_source=[], image="img", backend="local", python_bin="/fake/py",
                           expected_abi=_abi(), run=stub, work_root=tmp_path)
        msg = str(ei.value)
        assert "undeclared name not builtin: move_of" in msg      # the thing you actually need
        assert "src/native/evo/world.py:72:17" in msg             # ...and where
        assert "_base.py" not in msg                              # ...without the plumbing

    def test_compiler_digest_falls_back_to_the_tail_when_there_is_no_diagnostic(self):
        """A linker / setup.py failure emits no `file:line:col:` line — there the tail IS the story,
        so the old behaviour must survive rather than the digest swallowing it."""
        assert B._compiler_digest("ld: cannot find -lfoo\ncollect2: ld returned 1") \
            .endswith("collect2: ld returned 1")
        assert B._compiler_digest("") == ""

    def test_compiler_digest_dedupes_and_caps(self):
        """`nthreads` repeats the same diagnostic per worker, and a broken header can emit hundreds;
        an unbounded join would make the failure message itself unreadable."""
        dup = "\n".join(["src/a.py:1:1: bad thing"] * 5)
        assert B._compiler_digest(dup) == "src/a.py:1:1: bad thing"
        many = "\n".join(f"src/f{i}.py:{i}:1: broken" for i in range(30))
        out = B._compiler_digest(many, max_diags=8)
        assert out.count(" ; ") == 8 and out.endswith("more)")

    @pytest.mark.slow
    def test_real_local_compile_produces_importable_so(self, tmp_path):
        """End-to-end local backend: build a real Cython venv, compile a 2-module package, and
        import the compiled .so. Proves the driver + placement fix for real (the manual paid-smoke
        precursor); marked slow because it pip-installs cython and invokes gcc."""
        import subprocess as sp
        import venv as venvmod
        venvdir = tmp_path / "buildenv"
        venvmod.create(venvdir, with_pip=True)
        vpy = venvdir / "bin" / "python"
        r = sp.run([str(vpy), "-m", "pip", "install", "-q", "cython", "setuptools"],
                   capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"could not build cython venv: {r.stderr[-200:]}")
        code = _code_tar({"src/pkg/__init__.py": b"", "src/pkg/leaf.py": b"def val():\n    return 42\n",
                          "src/pkg/entry.py": b"from pkg.leaf import val\nprint(val())\n"})
        out_tar = B.compile_tree(code, packages=["src/pkg"], keep_source=["src/pkg/entry.py"],
                                 image="img", backend="local", python_bin=str(vpy),
                                 expected_abi=_abi(), run=sp.run, work_root=tmp_path)
        dest = tmp_path / "out"
        dest.mkdir()
        with tarfile.open(fileobj=io.BytesIO(out_tar), mode="r:gz") as tf:
            tf.extractall(dest)
        so = list((dest / "src" / "pkg").glob("leaf.*.so"))
        assert so, "leaf.py should have compiled to a .so"
        assert not (dest / "src" / "pkg" / "leaf.py").exists()
        assert (dest / "src" / "pkg" / "entry.py").exists()  # entry kept as source
        # the compiled leaf imports and runs under the SAME interpreter (matched ABI)
        got = sp.run([str(vpy), "-c", "import sys; sys.path.insert(0, r'%s'); "
                      "import pkg.leaf as l; print(l.val())" % str(dest / "src")],
                     capture_output=True, text=True)
        assert got.returncode == 0 and got.stdout.strip() == "42", got.stderr[-300:]

    def test_no_cross_module_cython_inputs(self):
        """⛔ THE GUARD UNDER THE PER-MODULE .so CACHE (spec inv. 7c).

        That cache keys each module's `.so` on its OWN source plus the toolchain, and cythonizes
        only the modules whose key is absent. That key is COMPLETE only while no module's generated
        C can depend on another file — i.e. while the tree has no `.pxd`, no `cimport`, and no
        `# cython:` directive comment. Add one and the key silently ships a STALE BINARY, which
        `docs/specs/task-dispatcher.spec.md` names the worst failure class this fleet has.

        So this asserts the premise rather than trusting it. If it goes red the fix is NOT to delete
        the assertion: either key on the whole dependency set, or bump `bundle.SO_CACHE_VER` and
        pass `cache_dir=None`."""
        import subprocess as sp
        files = sp.run(["git", "-C", str(ROOT), "ls-files", "src"],
                       capture_output=True, text=True)
        if files.returncode != 0:
            pytest.skip("not a git tree")
        pxd = [f for f in files.stdout.split() if f.endswith(".pxd")]
        assert not pxd, f"a .pxd makes the per-module .so cache key incomplete: {pxd}"
        offenders = []
        for rel in files.stdout.split():
            if not rel.endswith(".py"):
                continue
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("cimport ") or " cimport " in s or s.startswith("# cython:"):
                    offenders.append(f"{rel}:{i}: {s[:60]}")
        assert not offenders, (
            "these make one module's compilation depend on another file, which the per-module .so "
            "cache key (spec inv. 7c) does not cover:\n  " + "\n  ".join(offenders))

    @pytest.mark.slow
    def test_real_compile_reuses_cached_so_and_rebuilds_only_what_changed(self, tmp_path):
        """The per-module cache (spec inv. 7c), against the REAL compiler — a stub could not show
        that a cached `.so` is the one that gets shipped, nor that it still imports.

        Three builds of a 3-module package: cold (3 built), unchanged (0 built, 3 from cache), and
        one module edited (1 built, 2 from cache). The last is the case that matters — it is the
        median commit here, and today it recompiles all 93 modules."""
        import subprocess as sp
        import venv as venvmod
        venvdir = tmp_path / "buildenv"
        venvmod.create(venvdir, with_pip=True)
        vpy = venvdir / "bin" / "python"
        r = sp.run([str(vpy), "-m", "pip", "install", "-q", "cython", "setuptools"],
                   capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"could not build cython venv: {r.stderr[-200:]}")
        cache = tmp_path / "socache"
        seen = []

        def run_capturing(cmd, **kw):
            out = sp.run(cmd, **kw)
            if "-c" not in cmd:                      # the driver, not the EXT_SUFFIX probe
                seen.append(out.stdout or "")
            return out

        def build(a_src):
            code = _code_tar({"src/pkg/__init__.py": b"",
                              "src/pkg/a.py": a_src,
                              "src/pkg/b.py": b"def b():\n    return 2\n",
                              "src/pkg/c.py": b"def c():\n    return 3\n"})
            return B.compile_tree(code, packages=["src/pkg"], keep_source=[], image="img",
                                  backend="local", python_bin=str(vpy), expected_abi=_abi(),
                                  run=run_capturing, work_root=tmp_path, cache_dir=cache)

        def counts(stdout):
            import re
            m = re.search(r"\((\d+) from cache, (\d+) built\)", stdout)
            assert m, f"driver did not report cache counts: {stdout!r}"
            return int(m.group(1)), int(m.group(2))

        build(b"def a():\n    return 1\n")
        assert counts(seen[-1]) == (0, 3), "a cold build compiles everything"
        assert len(list(cache.glob("*.so"))) == 3, "each built module is published to the cache"

        build(b"def a():\n    return 1\n")
        assert counts(seen[-1]) == (3, 0), "an unchanged tree must compile NOTHING"

        out_tar = build(b"def a():\n    return 111\n")
        assert counts(seen[-1]) == (2, 1), "only the edited module recompiles"

        # ...and the shipped tar carries REAL bytes for the cached modules (copy, not symlink),
        # which import and return the NEW value for the one that changed.
        dest = tmp_path / "out"
        dest.mkdir()
        with tarfile.open(fileobj=io.BytesIO(out_tar), mode="r:gz") as tf:
            tf.extractall(dest)
        for name in ("a", "b", "c"):
            so = list((dest / "src" / "pkg").glob(f"{name}.*.so"))
            assert so and so[0].stat().st_size > 0 and not so[0].is_symlink()
            assert not (dest / "src" / "pkg" / f"{name}.py").exists()
        got = sp.run([str(vpy), "-c", "import sys; sys.path.insert(0, r'%s'); "
                      "import pkg.a, pkg.b, pkg.c as c; print(pkg.a.a(), pkg.b.b(), c.c())"
                      % str(dest / "src")], capture_output=True, text=True)
        assert got.returncode == 0 and got.stdout.strip() == "111 2 3", got.stderr[-400:]

    @pytest.mark.slow
    def test_real_compile_without_cache_dir_still_works(self, tmp_path):
        """`cache_dir=None` disables the cache — it is a pure accelerator, never a correctness
        dependency, so the un-cached path must stay exercised."""
        import subprocess as sp
        import venv as venvmod
        venvdir = tmp_path / "buildenv"
        venvmod.create(venvdir, with_pip=True)
        vpy = venvdir / "bin" / "python"
        r = sp.run([str(vpy), "-m", "pip", "install", "-q", "cython", "setuptools"],
                   capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"could not build cython venv: {r.stderr[-200:]}")
        code = _code_tar({"src/pkg/__init__.py": b"", "src/pkg/leaf.py": b"def val():\n    return 7\n"})
        out_tar = B.compile_tree(code, packages=["src/pkg"], keep_source=[], image="img",
                                 backend="local", python_bin=str(vpy), expected_abi=_abi(),
                                 run=sp.run, work_root=tmp_path, cache_dir=None)
        dest = tmp_path / "out"
        dest.mkdir()
        with tarfile.open(fileobj=io.BytesIO(out_tar), mode="r:gz") as tf:
            tf.extractall(dest)
        assert list((dest / "src" / "pkg").glob("leaf.*.so"))

    @pytest.mark.slow
    def test_real_compile_fails_loud_on_cython_incompatible_file(self, tmp_path):
        """Fail fast + loud (spec inv. 6): a single Cython-incompatible module (a mixed-type
        conditional expression — valid Python the static typer rejects) makes the WHOLE-tree compile
        raise BundleError, and the message names the offending file so the caller can surface it
        (never a silent success). The dispatcher turns this into a source fallback + `[ALERT]`.
        Reproduces the live 2026-07-24 fleet-wide fallback."""
        import subprocess as sp
        import venv as venvmod
        venvdir = tmp_path / "buildenv"
        venvmod.create(venvdir, with_pip=True)
        vpy = venvdir / "bin" / "python"
        r = sp.run([str(vpy), "-m", "pip", "install", "-q", "cython", "setuptools"],
                   capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"could not build cython venv: {r.stderr[-200:]}")
        # `bad.py` uses the exact idiom that broke the fleet: a conditional expr whose branches have
        # incompatible inferred C types (tuple vs float). Cython raises CompileError on it.
        bad = b"class W:\n    def col(self, rgb):\n        return (1.0, 1.0, 1.0) if rgb else 1.0\n"
        code = _code_tar({"src/pkg/__init__.py": b"", "src/pkg/good.py": b"def val():\n    return 7\n",
                          "src/pkg/bad.py": bad})
        with pytest.raises(B.CompileFailed, match="compile_tree failed") as ei:
            B.compile_tree(code, packages=["src/pkg"], keep_source=[], image="img",
                           backend="local", python_bin=str(vpy), expected_abi=_abi(),
                           run=sp.run, work_root=tmp_path)
        assert "bad.py" in str(ei.value)  # the offending file is named, not swallowed
