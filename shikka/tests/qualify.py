#!/usr/bin/env python3
"""Differential fixture: run on Linux; on Mac set SHIKKA_TEST_LIGHT=1."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

app = [sys.executable, "-m", "shikka"]
fixture = Path(__file__).parent / "fixture"
with tempfile.TemporaryDirectory(prefix="shikka-qualification-") as directory:
    # The Cargo workspace has a path dependency beside it, so the project root
    # is the directory above. Commands run in the workspace, as a developer's do.
    project = Path(directory).resolve() / "project"
    shutil.copytree(fixture, project)
    root = project / "workspace"
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    caller = dict(os.environ, FIXTURE_CALLER="visible")
    probe = dict(caller, FIXTURE_PROBE="seen")
    observations = []
    def run(command, success=True, cwd=root, env=caller, message=None):
        started = time.monotonic()
        result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
        observations.append({"command": command, "seconds": time.monotonic() - started, "returncode": result.returncode})
        if (result.returncode == 0) != success or (message and message not in result.stderr):
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout
    cargo = os.environ.get("SHIKKA_DEFAULT_CARGO", "cargo")
    run([cargo, "generate-lockfile", "--offline"])
    light = bool(os.environ.get("SHIKKA_TEST_LIGHT"))
    # The laptop's two build threads are this harness's choice, not a Shikka default.
    build = app + (["-j", "2"] if light else [])
    reference = "42:hello\n" if light else run([cargo, "run", "--offline", "-q"])
    probed = "42:hello\nseen:elsewhere\n" if light else run([cargo, "run", "--offline", "-q"], cwd=elsewhere, env=probe)
    if not light: run([cargo, "test", "--offline", "--workspace"])
    # A tool outside any immutable store, to replace in place below.
    packaged = json.loads(Path(os.environ["SHIKKA_TOOLCHAIN"]).read_text()) if os.environ.get("SHIKKA_TOOLCHAIN") else {}
    archiver = Path(directory) / "ar"
    archiver.write_text('#!/bin/sh\nexec "' + (packaged.get("ar") or shutil.which("ar")) + '" "$@"\n')
    archiver.chmod(0o755)
    try:
        # A root that cannot hold the path dependency is refused before
        # anything is written, and the refusal names one that can.
        run(app + ["--offline", "--ar", str(archiver), "init"], success=False, message="--root " + str(project))
        assert not (root / ".shikka").exists() and not (root / ".buckconfig").exists()
        run(app + ["--offline", "--ar", str(archiver), "--root", "..", "init"])
        assert run(build + ["--bin", "shikka-fixture", "run"]) == reference
        # Like Cargo, run starts the program where the caller is, with its environment.
        assert run(build + ["--bin", "shikka-fixture", "run"], cwd=elsewhere, env=probe) == probed
        run(build + ["test"])
        run(app + ["check"])
        # An option its command does not read is an error, not a silent no-op.
        run(app + ["--features", "unused", "build"], success=False, message="recorded by shikka init")
        run(app + ["--profile", "release", "test"], success=False, message="always uses Cargo's test profile")
        # Build/test failures must propagate, and failed compilation must not
        # poison restored incremental outputs.
        source = root / "src/main.rs"
        original = source.read_text()
        source.write_text(original + '\ncompile_error!("qualification failure");\n')
        run(build + ["build"], success=False)
        source.write_text(original)
        assert run(build + ["--bin", "shikka-fixture", "run"]) == reference
        test = root / "tests/contract.rs"
        original = test.read_text()
        test.write_text(original + '\n#[test] fn intentional_failure() { panic!("qualification"); }\n')
        run(build + ["test"], success=False)
        test.write_text(original)
        run(build + ["test"])
        # A tool replaced at its recorded path must stop builds until generate
        # probes the toolchain again.
        archiver.write_text(archiver.read_text() + "# replaced in place\n")
        run(build + ["build"], success=False, message="Toolchain files replaced on disk: ar")
        run(app + ["--offline", "generate"])
        run(app + ["check"])
        assert run(build + ["--bin", "shikka-fixture", "run"]) == reference
        # A new Cargo configuration must invalidate an already generated graph,
        # also where Cargo reads it and the project root is not: the workspace.
        (root / ".cargo").mkdir()
        (root / ".cargo/config.toml").write_text('[build]\nrustflags = ["--cfg", "unsupported"]\n')
        run(build + ["build"], success=False)
        print(json.dumps({"light": light, "reference": reference, "observations": observations}, indent=2))
    finally:
        config = project / ".shikka/project.json"
        if config.exists():
            buck = json.loads(config.read_text())["buck2"]
            subprocess.run([buck, "--isolation-dir", "shikka", "kill"], cwd=project, capture_output=True)
