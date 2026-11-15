"""Shikka's standalone Cargo frontend. Ordinary builds never invoke Cargo/Nix."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from . import __version__, generate, service, storage
from .project import RESOURCES, MARKER, atomic_write, cargo, executable, find_root, fingerprint, owned_write, read, requested_tools, toolchain, workspace_directory, write_json, validate_cargo_config

BUILD = {"build", "test", "run", "outputs"}
SELECT = BUILD | {"targets"}
# What `init` writes to project.json; later commands read it from there.
RECORDED = {"manifest_path", "rustc", "cargo", "cc", "cxx", "ar", "buck2", "features", "no_default_features"}
# The commands that read each option. Every parser option has an entry.
ACCEPTS = {
    **{name: {"init"} for name in RECORDED},
    "offline": {"init", "generate"},
    "cache": {"init"} | BUILD, "config_file": {"init"} | BUILD,
    "nativelink": {"init", "cache"} | BUILD, "state_dir": {"init", "cache"} | BUILD, "cache_bytes": {"init", "cache"} | BUILD,
    "cache_write": BUILD, "no_local_cas": BUILD, "local_content_store": BUILD, "jobs": BUILD, "buck_arg": BUILD,
    "pipelining": {"init"} | BUILD,
    "profile": SELECT - {"test"}, "package": SELECT, "bin": SELECT, "all_units": SELECT,
    "targets": SELECT | {"cache"},
    # Where the project is, not something project.json can change afterwards.
    "root": {"init"},
}


def reject_unused(args):
    """An option the command does not read is an error, not a silent no-op."""
    for name, commands in ACCEPTS.items():
        value = getattr(args, name)
        absent = value is None or value == [] or (value is False and name != "cache_write")
        if absent or args.command in commands:
            continue
        if name == "targets":
            raise ValueError(f"shikka {args.command} takes no targets: {' '.join(value)}")
        option = "--" + name.replace("_", "-")
        if name == "profile" and args.command == "test":
            raise ValueError("shikka test always uses Cargo's test profile; --profile does not apply.")
        if name in RECORDED:
            raise ValueError(f"{option} is recorded by shikka init and does not apply to shikka {args.command}; to change it, edit .shikka/project.json and run shikka generate.")
        raise ValueError(f"{option} does not apply to shikka {args.command}.")


def initialize(args):
    manifest = Path(args.manifest_path or "Cargo.toml").resolve(strict=True)
    # Cargo identifies the workspace root, including when invoked at a member.
    initial_cargo = executable(args.cargo or os.environ.get("SHIKKA_DEFAULT_CARGO"), "cargo")
    workspace = Path(json.loads(subprocess.check_output([initial_cargo, "locate-project", "--workspace", "--manifest-path", str(manifest), "--message-format", "json"], text=True))["root"]).parent.resolve()
    # Buck builds one tree. Path dependencies may live outside the Cargo
    # workspace, so the project root can be a directory above it.
    root = Path(args.root).resolve(strict=True) if args.root else workspace
    if not workspace.is_relative_to(root):
        raise ValueError("--root must contain the Cargo workspace " + str(workspace))
    if (root / ".shikka/project.json").exists():
        raise ValueError("Project is already initialized; use shikka generate, or edit .shikka/project.json.")
    if (root / ".buckconfig").exists() and not (root / ".buckconfig").read_text().startswith(MARKER):
        raise ValueError("Existing Buck project detected; this Cargo importer requires ownership of its generated .buckconfig/BUCK files.")
    for variable in ["RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"]:
        if os.environ.get(variable):
            raise ValueError(variable + " is not supported implicitly; use an explicit toolchain configuration.")
    requested, build_path = requested_tools(args)
    buck2 = executable(args.buck2 or os.environ.get("SHIKKA_DEFAULT_BUCK2"), "buck2")
    config = {"schema": 1, "manifest": manifest.relative_to(root).as_posix(), "workspace": generate.below(root, workspace), "buck2": buck2, "tools": requested, "build_path": build_path,
              "nativelink": args.nativelink or os.environ.get("SHIKKA_DEFAULT_NATIVELINK"), "cache": args.cache or "off", "pipelining": args.pipelining or "auto",
              "state_dir": str(Path(args.state_dir).expanduser().resolve()) if args.state_dir else None, "cache_bytes": args.cache_bytes, "config_file": str(Path(args.config_file).expanduser().resolve()) if args.config_file else None,
              "features": args.features or "", "no_default_features": args.no_default_features,
              "profiles": ["dev", "release", "test"]}
    tools = toolchain(config, root)
    validate_cargo_config(workspace, tools)
    # Ask Cargo where the local packages are before writing anything, so that
    # a root that cannot hold them leaves no half-initialized project behind.
    located = ["metadata", "--format-version", "1", "--locked", "--manifest-path", str(manifest)] + (["--offline"] if args.offline else [])
    generate.require_inside(json.loads(cargo(root, config, tools, located, vendored=False)), root)
    directory = root / ".shikka"
    directory.mkdir(mode=0o700, exist_ok=True)
    write_json(directory / "toolchain.json", tools)
    write_json(directory / "project.json", config)
    # Cargo's own target directory is in the workspace; Buck need not watch it.
    target = (config["workspace"] + "/" if config["workspace"] else "") + "target"
    # Bundled prelude provides only the standard source/export providers. Rust
    # compilation is our Cargo unit rule; no second dependency resolver exists.
    owned_write(root, ".buckconfig", MARKER + "\n" + f"""[cells]
root = .
shikka = .shikka/rules
prelude = prelude
none = none
toolchains = .shikka/toolchains
[cell_aliases]
config = prelude
ovr_config = prelude
fbcode = none
fbsource = none
fbcode_macros = none
buck = none
[external_cells]
prelude = bundled
[parser]
target_platform_detector_spec = target:root//...->prelude//platforms:default target:prelude//...->prelude//platforms:default
[build]
execution_platforms = shikka//:execution
[buck2]
restarter = true
clean_stale_enabled = true
clean_stale_low_disk_threshold = 10
clean_stale_low_disk_artifact_ttl_hours = 12
defer_write_actions = true
[project]
ignore = .git {target} buck-out .shikka/cargo-target .shikka/test-tmp
""")
    (directory / "toolchains").mkdir(exist_ok=True)
    owned_write(root, ".shikka/toolchains/BUCK", MARKER + "\n")
    try:
        refresh(root, config, tools, offline=args.offline)
    except BaseException:
        # project.json is what marks a project initialized, and init refuses
        # to replace it. A failed init must be one that can be run again.
        (directory / "project.json").unlink(missing_ok=True)
        raise
    print("Initialized " + str(root))


def regenerate(root, config, recorded, offline=False):
    """Probe the toolchain again, then import Cargo's graph with it."""
    tools = toolchain(config, root, recorded)
    changed = sorted(key for key in recorded.keys() | tools.keys() if recorded.get(key) != tools.get(key))
    if changed:
        write_json(root / ".shikka/toolchain.json", tools)
        print("Toolchain changed (" + ", ".join(changed) + "); its actions will rebuild.")
    return refresh(root, config, tools, offline=offline)


def refresh(root, config, tools, offline=False):
    validate_cargo_config(workspace_directory(root, config), tools)
    rules = root / ".shikka/rules"
    rules.mkdir(exist_ok=True)
    for resource in RESOURCES.iterdir():
        if resource.is_file():
            atomic_write(rules / resource.name, resource.read_text(), 0o644)
    command = ["vendor", "--locked", "--versioned-dirs", "--manifest-path", str(root / config["manifest"])]
    if offline: command.append("--offline")
    command.append(str(root / ".shikka/vendor"))
    vendor_config = cargo(root, config, tools, command, vendored=False)
    atomic_write(root / ".shikka/vendor.toml", vendor_config)
    state = generate.generate(root, config, tools)
    # Keep generated files out of commits without changing the project's
    # versioned ignore policy. Existing exclusions remain intact.
    result = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, text=True, capture_output=True)
    if result.returncode == 0:
        exclude = Path(result.stdout.strip())
        if not exclude.is_absolute(): exclude = root / exclude
        existing = exclude.read_text() if exclude.exists() else ""
        additions = ["/.shikka/", "/.buckconfig", "/buck-out/"] + ["/" + p for p in state["generated"] if not p.startswith(".shikka/")]
        missing = [p for p in additions if p not in existing.splitlines()]
        if missing:
            atomic_write(exclude, existing.rstrip() + "\n# Shikka generated files\n" + "\n".join(missing) + "\n")
    return state


def select(state, args):
    profile = "test" if args.command == "test" else args.profile or "dev"
    targets = state["units" if args.all_units else "targets"][profile]
    if args.package: targets = [t for t in targets if t["package"] in args.package]
    if args.bin: targets = [t for t in targets if "bin" in t["kind"] and t["target"] == args.bin]
    if args.command == "test": targets = [t for t in targets if t["mode"] == "test"]
    if args.targets:
        known = {t["label"]: t for t in state["units"][profile]}
        targets = [known[label] for label in args.targets]
    if not targets: raise ValueError("No Cargo targets match this selection")
    if args.command == "run" and (len(targets) != 1 or "bin" not in targets[0]["kind"]):
        raise ValueError("Select one executable with --bin NAME or an explicit target")
    return targets


# With fewer CPUs than this, `--pipelining auto` leaves it off.
PIPELINING_CPUS = 64


def available_cpus():
    """The CPUs this process may use, which is how many actions Buck2 runs at once."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # Not Linux.
        return os.cpu_count() or 1


def pipelined(choice, cpus):
    """Whether eligible libraries get an action that produces metadata alone.

    Libraries above it then start when that is written, not when its code is
    generated, which shortens every chain of libraries. The price is each
    library's analysis run twice. That is idle time put to use where there
    are more CPUs than the build can fill, and a slower build where there
    are not, so by default their number decides. It is the machine's, not
    `--jobs`: the choice is part of what gets built, and one that followed an
    option would rebuild whenever a command left the option out.
    """
    if choice in ("on", "off"):
        return choice == "on"
    return cpus >= PIPELINING_CPUS


def buck_command(config, args, targets, private, passthrough, snapshot=None):
    operation = "build" if args.command == "outputs" else args.command
    command = [config["buck2"], "--isolation-dir", "shikka", operation, *[t["label"] for t in targets], "--config-file", str(private)]
    if args.jobs: command += ["--num-threads", str(args.jobs)]
    command += args.buck_arg
    if args.command == "outputs": command += ["--show-full-json-output"]
    # After `--`, `run` arguments reach the program and `test` arguments reach
    # Buck2's test runner, which sets the environment tests start with.
    runner = ["--env", "SHIKKA_ENVIRONMENT=" + str(snapshot)] if snapshot else []
    if runner or passthrough: command += ["--", *runner, *passthrough]
    return command


@contextlib.contextmanager
def environment_snapshot(root, command):
    """Cargo starts tests with the caller's environment. Buck2's daemon starts
    them here and does not have it, so `shikka test` leaves run.py a private
    snapshot and removes it when the command ends."""
    if command != "test":
        yield None
        return
    directory = root / "buck-out/shikka"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="test-environment-", dir=directory)  # Created with mode 0600.
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(dict(os.environ), stream)
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def invoke(root, config, tools, args, passthrough):
    validate_cargo_config(workspace_directory(root, config), tools)
    state = generate.current(root)
    targets = select(state, args)
    if args.command == "targets":
        print(json.dumps(targets, indent=2))
        return 0
    mode = args.cache or config["cache"]
    writer = mode == "managed" if args.cache_write is None else args.cache_write
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("BUCK2_LOCAL_") or key in {"BUCK2_EXTRA_EXTERNAL_CONFIG", "BUCK2_DAEMON_DIR"}:
            env.pop(key)
    if args.command == "run":
        # Cargo runs the program where the user is. Buck2 is run from the root.
        env["SHIKKA_RUN_CWD"] = os.getcwd()
    cache_directory = service.state_directory(args.state_dir or config["state_dir"]) if mode == "managed" else None
    lease = service.lease(cache_directory, args.nativelink or config["nativelink"], args.cache_bytes or config["cache_bytes"]) if mode == "managed" else contextlib.nullcontext(None)
    with lease as local:
        content = f"""[shikka]
python = {tools['python']}
toolchain = {fingerprint(tools)}
os = {'macos' if sys.platform == 'darwin' else 'linux'}
arch = {platform.machine()}
cache = {mode}
cache_write = {str(writer).lower()}
pipelining = {str(pipelined(args.pipelining or config.get("pipelining", "auto"), available_cpus())).lower()}
"""
        content_dirs = []
        journal = None
        if local:
            content += f"""[buck2_re_client]
engine_address = 127.0.0.1:{local['port']}
action_cache_address = 127.0.0.1:{local['port']}
cas_address = 127.0.0.1:{local['port']}
tls = true
tls_ca_certs = {cache_directory / 'tls/ca.crt'}
tls_client_cert = {cache_directory / 'tls/client.pem'}
instance_name = shikka
"""
            if not args.no_local_cas:
                content_dirs = [Path(local["cas"])]
                env["BUCK2_LOCAL_CAS_DIR"] = local["cas"]
                if writer:
                    journal = root / "buck-out/shikka/local-cas-journal"
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    env["BUCK2_LOCAL_CAS_JOURNAL"] = str(journal)
        private_config = args.config_file or config["config_file"]
        if mode == "remote":
            if not private_config:
                raise ValueError("Remote cache mode requires --config-file with the server's Buck2 configuration")
            path = Path(private_config).expanduser().resolve(strict=True)
            if any(c in str(path) for c in "\n\r<>"):
                raise ValueError("Unsupported configuration path")
            content += "\n<file:" + str(path) + ">\n"
        private = root / ".shikka/client.buckconfig"
        atomic_write(private, content)
        env["BUCK2_EXTRA_EXTERNAL_CONFIG"] = str(private)
        # Use the existing filesystem capability probe and Linux dedupe code.
        # Environment mutation is scoped to this single-threaded CLI process.
        old_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            storage.configure_content_store(root, SimpleNamespace(local_content_store=args.local_content_store or "auto",
                local_content_store_max_bytes=None, local_content_store_max_entries=None, isolation_dir="shikka"))
            env = dict(os.environ)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        store = env.get("BUCK2_LOCAL_CONTENT_STORE")
        before = Path(store).stat().st_mtime_ns if store else None
        # Which action showed a library's errors, per build (action.py); none outlives its build.
        shutil.rmtree(root / "buck-out/shikka/reported", ignore_errors=True)
        with environment_snapshot(root, args.command) as snapshot:
            result = subprocess.run(buck_command(config, args, targets, private, passthrough, snapshot), cwd=root, env=env)
        if content_dirs and journal:
            changed = bool(store) and Path(store).stat().st_mtime_ns != before
            shared = storage.share_with_cas(content_dirs, changed, journal, store)
            if shared: return shared
        return result.returncode


def parser():
    # No option defaults here: reject_unused tells a given option from an absent one.
    p = argparse.ArgumentParser(description="Cargo resolves; Shikka builds, caches and shares artifacts.")
    p.add_argument("--version", action="version", version="shikka " + __version__)
    p.add_argument("--manifest-path", help="init only (default Cargo.toml)")
    p.add_argument("--root", help="init only: the project root, when path dependencies lie outside the Cargo workspace (default: the workspace)")
    for key in ["buck2", "nativelink", "rustc", "cargo", "cc", "cxx", "ar"]: p.add_argument("--" + key)
    p.add_argument("--profile", choices=["dev", "release"], help="default dev; test always uses Cargo's test profile")
    p.add_argument("-p", "--package", action="append", default=[])
    p.add_argument("--bin")
    p.add_argument("--jobs", "-j", type=int)
    p.add_argument("--features", help="init only")
    p.add_argument("--no-default-features", action="store_true", help="init only")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--cache", choices=["off", "managed", "remote"])
    p.add_argument("--cache-write", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--state-dir")
    p.add_argument("--cache-bytes", type=int, default=None)
    p.add_argument("--config-file")
    p.add_argument("--no-local-cas", action="store_true", help="Use cache transport without filesystem sharing")
    p.add_argument("--local-content-store", help="auto (default), off, or a directory")
    p.add_argument("--pipelining", choices=["auto", "on", "off"], help=f"compile eligible libraries' metadata in an action of its own, so the libraries above them start sooner; auto (default) is on where there are {PIPELINING_CPUS} CPUs or more; init records the choice")
    p.add_argument("--all-units", action="store_true")
    p.add_argument("--buck-arg", action="append", default=[])
    p.add_argument("command", choices=["init", "generate", "check", "build", "test", "run", "outputs", "targets", "cache"])
    p.add_argument("targets", nargs="*")
    return p


def main():
    try:
        argv = sys.argv[1:]
        passthrough = []
        if "--" in argv:
            index = argv.index("--")
            argv, passthrough = argv[:index], argv[index + 1:]
        args = parser().parse_args(argv)
        reject_unused(args)
        if passthrough and args.command not in {"run", "test"}:
            raise ValueError(f"Arguments after -- apply only to shikka run and shikka test, not shikka {args.command}.")
        if args.command == "init":
            args.cache_bytes = args.cache_bytes or 10 * 1024**3
            initialize(args)
            return
        if args.command == "cache":
            state = service.state_directory(args.state_dir)
            operation = args.targets[0] if len(args.targets) == 1 else "status"
            if operation == "start": result = service.ensure(state, args.nativelink, args.cache_bytes or 10 * 1024**3)
            elif operation == "status": result = service.status(state)
            elif operation == "stop": result = service.stop_service(state)
            else: raise ValueError("Use cache start, cache status, or cache stop")
            print(json.dumps(result, indent=2))
            return
        root = find_root()
        config = read(root / ".shikka/project.json")
        tools = read(root / ".shikka/toolchain.json")
        if args.command == "generate":
            regenerate(root, config, tools, offline=args.offline)
            print("Cargo graph regenerated")
            return
        if args.command == "check":
            state = generate.current(root)
            for path, expected in state["generated"].items():
                if not (root / path).is_file() or hashlib.sha256((root / path).read_bytes()).hexdigest() != expected:
                    raise ValueError("Generated file changed: " + path)
            print("Cargo graph and generated files match")
            return
        raise SystemExit(invoke(root, config, tools, args, passthrough))
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("shikka: " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
