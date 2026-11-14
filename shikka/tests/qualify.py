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
    root = Path(directory)
    shutil.copytree(fixture, root, dirs_exist_ok=True)
    observations = []
    def run(command, success=True):
        started = time.monotonic()
        result = subprocess.run(command, cwd=root, text=True, capture_output=True)
        observations.append({"command": command, "seconds": time.monotonic() - started, "returncode": result.returncode})
        if (result.returncode == 0) != success:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout
    cargo = os.environ.get("SHIKKA_DEFAULT_CARGO", "cargo")
    run([cargo, "generate-lockfile", "--offline"])
    light = bool(os.environ.get("SHIKKA_TEST_LIGHT"))
    reference = "42:hello\n" if light else run([cargo, "run", "--offline", "-q"])
    if not light: run([cargo, "test", "--offline", "--workspace"])
    try:
        run(app + ["--offline", "init"])
        assert run(app + ["--bin", "shikka-fixture", "run"]) == reference
        run(app + ["test"])
        run(app + ["check"])
        # Build/test failures must propagate, and failed compilation must not
        # poison restored incremental outputs.
        source = root / "src/main.rs"
        original = source.read_text()
        source.write_text(original + '\ncompile_error!("qualification failure");\n')
        run(app + ["build"], success=False)
        source.write_text(original)
        assert run(app + ["--bin", "shikka-fixture", "run"]) == reference
        test = root / "tests/contract.rs"
        original = test.read_text()
        test.write_text(original + '\n#[test] fn intentional_failure() { panic!("qualification"); }\n')
        run(app + ["test"], success=False)
        test.write_text(original)
        run(app + ["test"])
        # A new Cargo configuration must invalidate an already generated graph.
        (root / ".cargo").mkdir()
        (root / ".cargo/config.toml").write_text('[build]\nrustflags = ["--cfg", "unsupported"]\n')
        run(app + ["build"], success=False)
        print(json.dumps({"light": light, "reference": reference, "observations": observations}, indent=2))
    finally:
        config = root / ".shikka/project.json"
        if config.exists():
            buck = json.loads(config.read_text())["buck2"]
            subprocess.run([buck, "--isolation-dir", "shikka", "kill"], cwd=root, capture_output=True)
