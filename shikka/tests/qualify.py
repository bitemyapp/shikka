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
    def library_metadata():
        """Each pipelined library's metadata, as its metadata action left it and as its compilation did."""
        found = {"metadata": {}, "unit": {}}
        for path in (project / "buck-out/shikka").rglob("*.rmeta"):
            if path.parent.name in found:
                found[path.parent.name].setdefault(path.name, set()).add(path.read_bytes())
        return found["metadata"], found["unit"]
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
        # With pipelining, libraries above a library compile against metadata
        # that an action of its own writes, without generating code. The
        # library's compilation writes the same file beside its objects, and
        # the two must be the same bytes, or what was compiled against one
        # could not link the other. The default depends on the machine, so
        # ask for both; without pipelining there is no such file to compare.
        for choice in ["off", "on"]:
            assert run(build + ["--pipelining", choice, "--bin", "shikka-fixture", "run"]) == reference
            run(build + ["--pipelining", choice, "test"])
        early, complete = library_metadata()
        assert early and all(len(copies) == 1 and complete.get(name) == copies for name, copies in early.items()), sorted(early)
        # Selecting only a library must still finish its dependencies' code
        # generation. An invalid instruction reaches the assembler only during
        # code generation, so metadata alone succeeds. Both supported native
        # architectures (x86_64 and aarch64) accept Rust's inline asm syntax.
        leaf = root / "leaf/src/lib.rs"
        original = leaf.read_text()
        leaf.write_text(original + '''
pub fn codegen_failure() { unsafe { std::arch::asm!("shikka_invalid_instruction"); } }
''')
        try:
            if not light:
                run([cargo, "build", "--offline", "-p", "helper"], success=False, message="shikka_invalid_instruction")
            for choice in ["on", "off"]:
                run(build + ["--pipelining", choice, "-p", "helper", "build"], success=False, message="shikka_invalid_instruction")
        finally:
            leaf.write_text(original)
        run(build + ["--pipelining", "on", "-p", "helper", "build"])
        # init recorded the choice; a command's own option overrides it for that command.
        settings = project / ".shikka/project.json"
        recorded = settings.read_text()
        assert json.loads(recorded)["pipelining"] == "auto"
        chosen = lambda: "pipelining = true" in (project / ".shikka/client.buckconfig").read_text()
        for choice in ["on", "off"]:
            settings.write_text(json.dumps(dict(json.loads(recorded), pipelining=choice)))
            run(app + ["--offline", "generate"])
            run(build + ["build"])
            assert chosen() == (choice == "on")
            run(build + ["--pipelining", "off" if choice == "on" else "on", "build"])
            assert chosen() == (choice == "off")
        settings.write_text(recorded)
        run(app + ["--offline", "generate"])
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
