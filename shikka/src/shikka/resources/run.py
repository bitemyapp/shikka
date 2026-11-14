#!/usr/bin/env python3
"""Run declared Cargo outputs without changing compiler action inputs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

LIBRARY_PATH = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
# Set by the Shikka CLI for Buck2 or for this script, not for the program.
PRIVATE = ("BUCK2_LOCAL_", "BUCK2_EXTRA_EXTERNAL_CONFIG", "SHIKKA_RUN_CWD", "SHIKKA_ENVIRONMENT")


def invoking_environment(environ, test_run, declared):
    """The environment Cargo would start the program with: the caller's own.

    `shikka run` inherits it through Buck2's exec. Tests are started by the
    Buck2 daemon, which does not have it, so `shikka test` names a private
    snapshot in SHIKKA_ENVIRONMENT. A test started without one (plain
    `buck2 test`) gets the toolchain's declared environment instead.
    """
    if not test_run:
        return {key: value for key, value in environ.items() if not key.startswith(PRIVATE)}
    snapshot = environ.get("SHIKKA_ENVIRONMENT")
    if snapshot:
        return json.loads(Path(snapshot).read_text())
    env = declared()
    env.update({key: value for key, value in environ.items() if key.startswith(("XDG_", "WAYLAND_", "DBUS_")) or key in {"HOME", "TMPDIR", "RUST_BACKTRACE"}})
    return env


def runtime_environment(env, tools, info, runtime):
    """Add what Cargo sets for a program it runs: its package and library paths."""
    inherited = env.get(LIBRARY_PATH, "")
    env = dict(env, CARGO=tools["cargo"])
    env.update(info["cargo_environment"])
    env.update(runtime)
    # An empty entry would make the loader search the working directory.
    env[LIBRARY_PATH] = ":".join(path for path in [*info["library_paths"], tools["rustlib"], runtime.get(LIBRARY_PATH, ""), inherited] if path)
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--action-module", required=True)
    parser.add_argument("--test-run", action="store_true")
    argv = sys.argv[1:]
    arguments = []
    if "--" in argv:
        delimiter = argv.index("--")
        argv, arguments = argv[:delimiter], argv[delimiter + 1:]
    args = parser.parse_args(argv)
    # Buck exports these source artifacts into separate directories.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(args.action_module).absolute().parent))
    from action import load, native_environment, relocate

    tools = load(args.environment)
    root = Path.cwd()
    output = Path(args.run).absolute()
    info = relocate(load(output / "unit.json"), root, output)
    runtime = load(args.runtime) if args.runtime else {}
    env = runtime_environment(invoking_environment(os.environ, args.test_run, lambda: native_environment(tools)), tools, info, runtime)
    if info.get("test_tmp"):
        (root / info["test_tmp"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        env["CARGO_TARGET_TMPDIR"] = str(root / info["test_tmp"])
    command = [str(output / info["filename"]), *arguments]
    if args.test_run:
        with tempfile.TemporaryDirectory(prefix="sb-", dir="/tmp") as private:
            for name in ["home", "run", "tmp", "target"]:
                Path(private, name).mkdir(mode=0o700)
            env.update(HOME=private + "/home", XDG_RUNTIME_DIR=private + "/run", TMPDIR=private + "/tmp", CARGO_TARGET_DIR=private + "/target", DBUS_SESSION_BUS_ADDRESS="unix:path=" + private + "/no-session-bus", DBUS_SYSTEM_BUS_ADDRESS="unix:path=" + private + "/no-system-bus")
            raise SystemExit(subprocess.run(command, env=env, cwd=info["manifest"]).returncode)
    # Every path above is absolute, so the program can start where the user is.
    if os.environ.get("SHIKKA_RUN_CWD"):
        os.chdir(os.environ["SHIKKA_RUN_CWD"])
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
