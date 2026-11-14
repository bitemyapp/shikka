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
import time
from types import SimpleNamespace

from . import __version__, generate, service, storage
from .project import RESOURCES, MARKER, atomic_write, cargo, executable, find_root, fingerprint, owned_write, read, toolchain, write_json, validate_cargo_config


def initialize(args):
    manifest = Path(args.manifest_path).resolve(strict=True)
    # Cargo identifies the workspace root, including when invoked at a member.
    initial_cargo = executable(args.cargo or os.environ.get("SHIKKA_DEFAULT_CARGO"), "cargo")
    root = Path(json.loads(subprocess.check_output([initial_cargo, "locate-project", "--workspace", "--manifest-path", str(manifest), "--message-format", "json"], text=True))["root"]).parent.resolve()
    if (root / ".shikka/project.json").exists():
        raise ValueError("Project is already initialized; use shikka generate, or edit .shikka/project.json.")
    if (root / ".buckconfig").exists() and not (root / ".buckconfig").read_text().startswith(MARKER):
        raise ValueError("Existing Buck project detected; this Cargo importer requires ownership of its generated .buckconfig/BUCK files.")
    for variable in ["RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"]:
        if os.environ.get(variable):
            raise ValueError(variable + " is not supported implicitly; use an explicit toolchain configuration.")
    tools = toolchain(args, root)
    validate_cargo_config(root, tools)
    buck2 = executable(args.buck2 or os.environ.get("SHIKKA_DEFAULT_BUCK2"), "buck2")
    config = {"schema": 1, "manifest": manifest.relative_to(root).as_posix(), "buck2": buck2,
              "nativelink": args.nativelink or os.environ.get("SHIKKA_DEFAULT_NATIVELINK"), "cache": args.cache or "off",
              "state_dir": str(Path(args.state_dir).expanduser().resolve()) if args.state_dir else None, "cache_bytes": args.cache_bytes, "config_file": str(Path(args.config_file).expanduser().resolve()) if args.config_file else None,
              "features": args.features, "no_default_features": args.no_default_features,
              "profiles": ["dev", "release", "test"]}
    directory = root / ".shikka"
    directory.mkdir(mode=0o700, exist_ok=True)
    write_json(directory / "toolchain.json", tools)
    write_json(directory / "project.json", config)
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
ignore = .git target buck-out .shikka/cargo-target .shikka/test-tmp
""")
    (directory / "toolchains").mkdir(exist_ok=True)
    owned_write(root, ".shikka/toolchains/BUCK", MARKER + "\n")
    refresh(root, config, tools, offline=args.offline)
    print("Initialized " + str(root))


def refresh(root, config, tools, offline=False):
    validate_cargo_config(root, tools)
    rules = root / ".shikka/rules"
    rules.mkdir(exist_ok=True)
    for resource in RESOURCES.iterdir():
        if resource.is_file():
            atomic_write(rules / resource.name, resource.read_text(), 0o644)
    command = ["vendor", "--locked", "--versioned-dirs", "--manifest-path", config["manifest"]]
    if offline: command.append("--offline")
    command.append(str(root / ".shikka/vendor"))
    vendor_config = cargo(root, tools, command, vendored=False)
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
    profile = "test" if args.command == "test" else args.profile
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


def invoke(root, config, tools, args, passthrough):
    validate_cargo_config(root, tools)
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
            storage.configure_content_store(root, SimpleNamespace(local_content_store=args.local_content_store,
                local_content_store_max_bytes=None, local_content_store_max_entries=None, isolation_dir="shikka"))
            env = dict(os.environ)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        operation = "build" if args.command == "outputs" else args.command
        command = [config["buck2"], "--isolation-dir", "shikka", operation, *[t["label"] for t in targets], "--config-file", str(private)]
        if args.jobs: command += ["--num-threads", str(args.jobs)]
        elif sys.platform == "darwin": command += ["--num-threads", "2"]
        command += args.buck_arg
        if args.command == "outputs": command += ["--show-full-json-output"]
        if passthrough: command += ["--", *passthrough]
        store = env.get("BUCK2_LOCAL_CONTENT_STORE")
        before = Path(store).stat().st_mtime_ns if store else None
        result = subprocess.run(command, cwd=root, env=env)
        if content_dirs and journal:
            changed = bool(store) and Path(store).stat().st_mtime_ns != before
            shared = storage.share_with_cas(content_dirs, changed, journal, store)
            if shared: return shared
        return result.returncode


def parser():
    p = argparse.ArgumentParser(description="Cargo resolves; Shikka builds, caches and shares artifacts.")
    p.add_argument("--version", action="version", version="shikka " + __version__)
    p.add_argument("--manifest-path", default="Cargo.toml")
    for key in ["buck2", "nativelink", "rustc", "cargo", "cc", "cxx", "ar"]: p.add_argument("--" + key)
    p.add_argument("--profile", default="dev", choices=["dev", "release"])
    p.add_argument("-p", "--package", action="append", default=[])
    p.add_argument("--bin")
    p.add_argument("--jobs", "-j", type=int)
    p.add_argument("--features", default="")
    p.add_argument("--no-default-features", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--cache", choices=["off", "managed", "remote"])
    p.add_argument("--cache-write", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--state-dir")
    p.add_argument("--cache-bytes", type=int, default=None)
    p.add_argument("--config-file")
    p.add_argument("--no-local-cas", action="store_true", help="Use cache transport without filesystem sharing")
    p.add_argument("--local-content-store", default="auto")
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
            refresh(root, config, tools, offline=args.offline)
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
