"""Frontend contracts that need no engine: options, toolchain freshness, runtime environment."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from shikka import cli, generate, project

sys.path.insert(0, str(project.RESOURCES))
import action  # noqa: E402 (resources/action.py, as Buck runs it)
import run as launcher  # noqa: E402


class Options(unittest.TestCase):
    def parse(self, *argv):
        args = cli.parser().parse_args(list(argv))
        cli.reject_unused(args)
        return args

    def test_every_option_names_the_commands_that_read_it(self):
        self.assertEqual(set(cli.ACCEPTS), set(vars(cli.parser().parse_args(["build"]))) - {"command"})

    def test_options_recorded_by_init_are_rejected_afterwards(self):
        for option in (["--features", "x"], ["--no-default-features"], ["--manifest-path", "Cargo.toml"], ["--rustc", "rustc"], ["--buck2", "buck2"]):
            for command in ["build", "test", "run", "outputs", "targets", "generate", "check", "cache"]:
                with self.assertRaisesRegex(ValueError, "recorded by shikka init"):
                    self.parse(*option, command)
            self.parse(*option, "init")

    def test_options_a_command_does_not_read_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "always uses Cargo's test profile"):
            self.parse("--profile", "release", "test")
        for argv in (["--offline", "build"], ["-j", "2", "init"], ["--bin", "x", "generate"], ["--no-cache-write", "check"],
                     ["--cache", "managed", "targets"], ["--profile", "dev", "cache"], ["check", "//x:y"], ["--root", "/x", "generate"]):
            with self.assertRaises(ValueError, msg=argv):
                self.parse(*argv)
        for argv in (["--profile", "release", "build"], ["--offline", "generate"], ["-j", "2", "-p", "a", "--bin", "b", "test"],
                     ["--state-dir", "/x", "cache", "stop"], ["--cache", "managed", "--no-cache-write", "outputs"], ["targets", "//x:y"], ["--root", "/x", "init"]):
            self.parse(*argv)

    def test_threads_are_limited_only_on_request(self):
        config, targets = {"buck2": "/engine"}, [{"label": "//:unit"}]
        for platform in ["darwin", "linux"]:
            with mock.patch.object(sys, "platform", platform):
                self.assertNotIn("--num-threads", cli.buck_command(config, self.parse("build"), targets, "private", []))
        command = cli.buck_command(config, self.parse("-j", "3", "build"), targets, "private", [])
        self.assertEqual(command[command.index("--num-threads") + 1], "3")

    def test_pipelining_follows_the_machine_unless_told(self):
        self.assertTrue(cli.pipelined("on", 1))
        self.assertFalse(cli.pipelined("off", 256))
        self.assertFalse(cli.pipelined("auto", cli.PIPELINING_CPUS - 1))
        self.assertTrue(cli.pipelined("auto", cli.PIPELINING_CPUS))
        self.assertGreaterEqual(cli.available_cpus(), 1)
        # For one command, or to init as the project's choice.
        self.parse("--pipelining", "on", "test")
        self.parse("--pipelining", "off", "init")
        with self.assertRaises(ValueError):
            self.parse("--pipelining", "on", "generate")

    def test_test_runner_is_told_where_the_caller_environment_is(self):
        config, targets = {"buck2": "/engine"}, [{"label": "//:unit"}]
        command = cli.buck_command(config, self.parse("test"), targets, "private", ["--test-arg", "x"], "/snapshot")
        self.assertEqual(command[command.index("--"):], ["--", "--env", "SHIKKA_ENVIRONMENT=/snapshot", "--test-arg", "x"])
        self.assertNotIn("--", cli.buck_command(config, self.parse("build"), targets, "private", []))

    def test_caller_environment_snapshot_is_private_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with cli.environment_snapshot(root, "build") as snapshot:
                self.assertIsNone(snapshot)
            with mock.patch.dict(os.environ, {"FIXTURE_CALLER": "visible"}):
                with cli.environment_snapshot(root, "test") as snapshot:
                    self.assertEqual(stat.S_IMODE(os.stat(snapshot).st_mode), 0o600)
                    self.assertEqual(json.loads(Path(snapshot).read_text())["FIXTURE_CALLER"], "visible")
            self.assertFalse(Path(snapshot).exists())


class Toolchain(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "version").write_text("1.0.0")
        scripts = {
            "rustc": 'case "$*" in "--print sysroot") pwd > "$d/probed-in"; echo "$d/sysroot";; "-vV") printf "rustc %s\\nhost: test-host\\n" "$(cat "$d/version")";; "--print target-libdir") echo "$d/sysroot/lib";; esac',
            "cargo": 'echo "cargo $(cat "$d/version")"', "cc": "echo cc", "c++": "echo c++", "ar": "echo ar",
        }
        for name, body in scripts.items():
            path = self.root / name
            path.write_text('#!/bin/sh\nd=$(dirname "$0")\n' + body + "\n")
            path.chmod(0o755)
        self.args = SimpleNamespace(rustc=str(self.root / "rustc"), cargo=str(self.root / "cargo"), cc=str(self.root / "cc"), cxx=str(self.root / "c++"), ar=str(self.root / "ar"))
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SHIKKA_TOOLCHAIN", None)

    def test_generate_probes_the_recorded_choices_again(self):
        os.environ["PATH"] = "/init/bin" + os.pathsep + os.environ["PATH"]
        requested, build_path = project.requested_tools(self.args)
        config = {"tools": requested, "build_path": build_path}
        first = project.toolchain(config, self.root)
        self.assertIn("1.0.0", first["versions"]["rustc"])
        # The compiler is updated in place, and generate runs from another shell.
        (self.root / "version").write_text("2.0.0")
        os.environ["PATH"] = os.environ["PATH"].replace("/init/bin" + os.pathsep, "")
        second = project.toolchain(config, self.root, first)
        self.assertEqual(first["rustc"], second["rustc"])
        self.assertIn("2.0.0", second["versions"]["rustc"])
        self.assertNotEqual(project.fingerprint(first), project.fingerprint(second))
        self.assertEqual(first["BUILD_PATH"], second["BUILD_PATH"])
        self.assertTrue(first["BUILD_PATH"].startswith("/init/bin"))

    def test_packaged_toolchain_is_deferred_to_not_recorded(self):
        packaged = self.root / "packaged.json"
        packaged.write_text(json.dumps({key: getattr(self.args, key) for key in ["rustc", "cargo", "cc", "cxx", "ar"]} | {"BUILD_PATH": "/packaged/bin"}))
        os.environ["SHIKKA_TOOLCHAIN"] = str(packaged)
        requested, build_path = project.requested_tools(SimpleNamespace(rustc=None, cargo=None, cc=None, cxx=None, ar=str(self.root / "ar")))
        self.assertEqual(requested, {"rustc": None, "cargo": None, "cc": None, "cxx": None, "ar": str(self.root / "ar")})
        self.assertIsNone(build_path)
        tools = project.toolchain({"tools": requested, "build_path": build_path}, self.root)
        self.assertEqual((tools["rustc"], tools["BUILD_PATH"]), (self.args.rustc, "/packaged/bin"))

    def test_a_store_tool_keeps_the_name_it_was_asked_for_by(self):
        # rustc keeps its default linker for `cc` and drops it for `gcc`.
        store, profile = self.root / "store", self.root / "profile"
        (store / "wrapper/bin").mkdir(parents=True)
        profile.mkdir()
        (store / "wrapper/bin/gcc").write_text("compiler")
        (store / "wrapper/bin/cc").symlink_to("gcc")
        (profile / "cc").symlink_to(store / "wrapper/bin/cc")
        (profile / "system-cc").symlink_to(store / "wrapper/bin/gcc")
        self.assertEqual(project.pinned(str(profile / "cc"), store), str(store / "wrapper/bin/cc"))
        # No such name beside the file it leads to: the file itself.
        self.assertEqual(project.pinned(str(profile / "system-cc"), store), str(store / "wrapper/bin/gcc"))
        # Outside the store nothing is resolved; the path may be updated in place.
        (self.root / "usr-cc").symlink_to(self.root / "cc")
        self.assertEqual(project.pinned(str(self.root / "usr-cc"), store), str(self.root / "usr-cc"))

    def test_rustup_and_cargo_are_consulted_where_the_workspace_is(self):
        # With `init --root`, the workspace is below the project root. rustup
        # reads rust-toolchain.toml, and Cargo its configuration, from there up.
        workspace = self.root / "below/workspace"
        (workspace / ".cargo").mkdir(parents=True)
        requested, build_path = project.requested_tools(self.args)
        config = {"tools": requested, "build_path": build_path, "workspace": "below/workspace"}
        project.toolchain(config, self.root)
        self.assertEqual((self.root / "probed-in").read_text().strip(), str(workspace))
        (self.root / ".shikka").mkdir()
        state = {"inputs": {}, "workspace": "below/workspace", "cargo_configs": project.cargo_configs(workspace), "app_inputs": project.app_inputs(), "toolchain": {}}
        (self.root / ".shikka/graph.json").write_text(json.dumps(state))
        generate.current(self.root)
        (workspace / ".cargo/config.toml").write_text("[net]\noffline = true\n")
        with self.assertRaisesRegex(ValueError, "Cargo configuration files"):
            generate.current(self.root)
        self.assertIn(str(workspace / ".cargo/config.toml"), project.cargo_configs(workspace))
        self.assertNotIn(str(workspace / ".cargo/config.toml"), project.cargo_configs(self.root))

    def test_a_tool_replaced_on_disk_makes_the_graph_stale(self):
        (self.root / "real-ar").write_text("first")
        tool = self.root / "linked-ar"
        tool.symlink_to("real-ar")
        (self.root / ".shikka").mkdir()
        state = {"inputs": {}, "cargo_configs": project.cargo_configs(self.root), "app_inputs": project.app_inputs(),
                 "toolchain": {"ar": project.file_identity(str(tool))}}
        (self.root / ".shikka/graph.json").write_text(json.dumps(state))
        self.assertEqual(generate.current(self.root)["toolchain"], state["toolchain"])
        (self.root / "other-ar").write_text("first")
        for replace in [lambda: (self.root / "real-ar").write_text("second update"),
                        lambda: (tool.unlink(), tool.symlink_to("other-ar")),
                        lambda: tool.unlink()]:
            replace()
            with self.assertRaisesRegex(ValueError, "Toolchain files replaced on disk: ar"):
                generate.current(self.root)


PROFILE = {"name": "dev", "opt_level": "0", "debuginfo": 2, "debug_assertions": True, "overflow_checks": True, "panic": "unwind", "lto": "false", "incremental": True}


def cargo_package(name, directory, kind="lib", crate_types=None, source=None, script=False, metadata=None):
    """A package as `cargo metadata` describes it, and its targets as the unit graph does."""
    targets = {kind: {"name": name, "kind": [kind], "crate_types": crate_types or [kind], "src_path": directory + "/src/lib.rs", "edition": "2024"}}
    if script:
        targets["custom-build"] = {"name": "build-script-build", "kind": ["custom-build"], "crate_types": ["bin"], "src_path": directory + "/build.rs", "edition": "2024"}
    return {"id": name + " 0.1.0", "name": name, "version": "0.1.0", "source": source, "manifest_path": directory + "/Cargo.toml",
            "features": {}, "targets": list(targets.values()), "metadata": metadata}, targets


class Graph(unittest.TestCase):
    """Cargo's unit graph says what to build; these are the rustc flags it leaves to Cargo."""

    def graph(self, app_metadata=None):
        app, app_targets = cargo_package("app", "/project/workspace/app", "bin", script=True, metadata=app_metadata)
        member, member_targets = cargo_package("member", "/project/workspace/member", "dylib")
        outside, outside_targets = cargo_package("outside", "/project/outside", "dylib")
        macro, macro_targets = cargo_package("macro", "/project/workspace/macro", "proc-macro")
        meta = {"packages": [app, member, outside, macro], "workspace_root": "/project/workspace"}
        def unit(package, target, mode="build", dependencies=()):
            return {"pkg_id": package["id"], "target": target, "profile": PROFILE, "platform": None, "mode": mode, "features": [],
                    "dependencies": [{"index": index, "extern_crate_name": "dependency"} for index in dependencies]}
        units = {
            "app": unit(app, app_targets["bin"], dependencies=[1, 2, 3, 6]),
            "member": unit(member, member_targets["dylib"]),
            "outside": unit(outside, outside_targets["dylib"]),
            "macro": unit(macro, macro_targets["proc-macro"]),
            "macro test": unit(macro, macro_targets["proc-macro"], "test"),
            "app script": unit(app, app_targets["custom-build"]),
            "app script run": unit(app, app_targets["custom-build"], "run-custom-build", [5]),
            "member test": unit(member, member_targets["dylib"], "test"),
        }
        names = list(units)
        # `cargo build --workspace`: every member is asked for; `outside` is only a dependency.
        graph = {"units": list(units.values()), "roots": [names.index(name) for name in ["app", "member", "macro", "macro test", "member test"]]}
        prepared, _ = generate.units_from_graph(graph, generate.package_records(meta, Path("/project")), meta)
        return dict(zip(names, prepared))

    def test_std_is_linked_dynamically_where_cargo_does(self):
        units = self.graph()
        self.assertEqual({name for name, unit in units.items() if unit.get("prefer_dynamic")}, {"outside", "macro", "macro test"})
        self.assertEqual(action.profile_flags(units["outside"])[-2:], ["-C", "prefer-dynamic"])
        self.assertNotIn("prefer-dynamic", action.profile_flags(units["member"]))

    def test_a_package_gives_its_own_targets_flags(self):
        flags = ["-C", "prefer-dynamic"]
        units = self.graph({"shikka": {"rustflags": flags}})
        # Its targets, and its build script's environment; not the script's compilation.
        self.assertEqual({name for name, unit in units.items() if unit.get("rustflags")}, {"app", "app script run"})
        self.assertEqual(action.profile_flags(units["app"])[-2:], flags)
        plain = self.graph()
        self.assertNotEqual(units["app"]["key"], plain["app"]["key"])
        self.assertEqual(units["member"]["key"], plain["member"]["key"])

    def test_package_settings_are_checked(self):
        def settings(table, script=True, source=None):
            package, _ = cargo_package("app", "/project/app", script=script, source=source, metadata={"shikka": table})
            return generate.package_settings({"packages": [package]})
        self.assertEqual(settings({"build-script-reads-workspace": True}), {"app 0.1.0": {"rustflags": [], "reads_workspace": True}})
        # A dependency from a registry does not configure the project's build.
        self.assertEqual(settings({"rustflags": ["-O"]}, source="registry+https://example.invalid"), {})
        for table, message in [({"rustflag": []}, "accepts only"), ({"rustflags": "-O"}, "list of strings"), ({"rustflags": [1]}, "list of strings"),
                               ({"build-script-reads-workspace": "yes"}, "true or false"), ("rustflags", "accepts only")]:
            with self.assertRaisesRegex(ValueError, message):
                settings(table)
        with self.assertRaisesRegex(ValueError, "has no build script"):
            settings({"build-script-reads-workspace": True}, script=False)

    def test_a_local_package_outside_the_root_names_a_root_that_holds_it(self):
        app, _ = cargo_package("app", "/project/workspace/app")
        outside, _ = cargo_package("outside", "/project/outside")
        registry, _ = cargo_package("serde", "/home/cargo/registry/serde", source="registry+https://example.invalid")
        meta = {"packages": [app, outside, registry]}
        generate.require_inside(meta, Path("/project"))
        with self.assertRaisesRegex(ValueError, r"outside \(/project/outside\)\n.*--root /project\.$"):
            generate.require_inside(meta, Path("/project/workspace"))


class Libraries(unittest.TestCase):
    """With pipelining a library has two actions: one for its metadata, one for its objects."""

    def units(self):
        top, top_targets = cargo_package("top", "/project/top")
        base, base_targets = cargo_package("base", "/project/base")
        for package in [top, base]:
            package["authors"] = []  # As `cargo metadata` always gives it.
        meta = {"packages": [top, base], "workspace_root": "/project"}
        def unit(package, target, dependencies=()):
            return {"pkg_id": package["id"], "target": target, "profile": dict(PROFILE, incremental=False), "platform": None, "mode": "build", "features": [],
                    "dependencies": [{"index": index, "extern_crate_name": "base"} for index in dependencies]}
        graph = {"units": [unit(top, top_targets["lib"], [1]), unit(base, base_targets["lib"])], "roots": [0]}
        prepared, _ = generate.units_from_graph(graph, generate.package_records(meta, Path("/project")), meta)
        return prepared

    def compile(self, directory, pipelined=None, dependency=None):
        """compile_unit with a stand-in rustc: its arguments, how it was run, and what was left."""
        top, base = self.units()
        output = directory / "out"
        output.mkdir()
        payload = {"unit": top, "source": str(directory / "source"), "closure": [str(directory / "base")],
                   "dependencies": [dict({"directory": str(directory / "base"), "spec": base}, **(dependency or {}))]}
        def rustc(arguments, **_):
            # What rustc leaves for --emit=dep-info,metadata,link, with or without code generation.
            for name in [top["filename"], action.metadata_name(top), "rustc.d"]:
                (output / name).write_text(name)
            return subprocess.CompletedProcess(arguments, 0, stderr=b"")
        with mock.patch.object(action.subprocess, "run", side_effect=rustc) as run:
            action.compile_unit(payload, output, {}, {"rustc": "rustc", "cc": "cc"}, directory, None, None, pipelined)
        return run.call_args.args[0], run.call_args.kwargs, sorted(path.name for path in output.iterdir()), top, base

    def test_both_actions_ask_rustc_for_the_same_outputs(self):
        def kinds(arguments):
            emit = next(argument for argument in arguments if argument.startswith("--emit="))
            return sorted(kind.split("=")[0] for kind in emit.removeprefix("--emit=").split(","))
        with tempfile.TemporaryDirectory() as directory:
            objects, _, left, top, _ = self.compile(Path(directory).resolve(), "objects")
            # The library, and the metadata rustc wrote with it; its dependency list is not an output.
            self.assertEqual(left, sorted([top["filename"], action.metadata_name(top)]))
        with tempfile.TemporaryDirectory() as directory:
            metadata, _, left, top, _ = self.compile(Path(directory).resolve(), "metadata")
            # Only the metadata: the archive -Z no-codegen writes holds no objects.
            self.assertEqual(left, [action.metadata_name(top)])
        # The kinds are part of the crate hash: they must not differ between the two.
        self.assertEqual(kinds(objects), ["dep-info", "link", "metadata"])
        self.assertEqual(kinds(metadata), kinds(objects))
        self.assertNotIn("no-codegen", objects)
        self.assertEqual(metadata[metadata.index("no-codegen") - 1], "-Z")

    def test_a_library_reads_metadata_where_the_rule_says(self):
        for dependency, suffix in [({"metadata": True}, ".rmeta"), ({}, ".rlib")]:
            with tempfile.TemporaryDirectory() as directory:
                arguments, _, _, _, base = self.compile(Path(directory).resolve(), "objects", dependency)
                extern = arguments[arguments.index("--extern") + 1]
                self.assertTrue(extern.startswith("base=") and extern.endswith("/base/" + base["filename"].removesuffix(".rlib") + suffix), extern)

    def test_without_pipelining_a_library_is_compiled_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, how, left, top, _ = self.compile(Path(directory).resolve())
            self.assertIn("--emit=link", arguments)
            self.assertNotIn("no-codegen", arguments)
            self.assertIn(top["filename"], left)
            # Its errors go straight to stderr: there is no second action to defer to.
            self.assertIsNone(how["stderr"])
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.compile(Path(directory).resolve(), "objects")[1]["stderr"], subprocess.PIPE)

    def test_a_librarys_errors_are_shown_by_one_of_its_two_actions(self):
        top, _ = self.units()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(action.sys, "stderr") as stderr:
            scratch = os.path.join("buck-out", "shikka", "tmp", "root", "0", "cargo_build", "top")
            shown = lambda: b"".join(call.args[0] for call in stderr.buffer.write.call_args_list)
            pointed = lambda: [call.args[0] for call in stderr.write.call_args_list if "other failed action" in call.args[0]]
            previous = os.getcwd()
            os.chdir(directory)
            try:
                with mock.patch.dict(os.environ, {"BUCK_BUILD_ID": "build-1", "BUCK_SCRATCH_PATH": scratch}):
                    action.CompileFailed(top, 1, b"error: first\n").report()
                    self.assertEqual((shown(), pointed()), (b"error: first\n", []))
                    # The same library failing again in the same build: the other action.
                    action.CompileFailed(top, 1, b"error: second\n").report()
                    self.assertEqual((shown(), len(pointed())), (b"error: first\n", 1))
                # A later build starts over, as does an action Buck2 did not name a build to.
                with mock.patch.dict(os.environ, {"BUCK_BUILD_ID": "build-2", "BUCK_SCRATCH_PATH": scratch}):
                    action.CompileFailed(top, 1, b"error: third\n").report()
                with mock.patch.dict(os.environ, {"BUCK_SCRATCH_PATH": scratch}):
                    os.environ.pop("BUCK_BUILD_ID", None)
                    action.CompileFailed(top, 1, b"error: fourth\n").report()
                    action.CompileFailed(top, 1, b"error: fifth\n").report()
                self.assertEqual((shown(), len(pointed())), (b"error: first\nerror: third\nerror: fourth\nerror: fifth\n", 1))
            finally:
                os.chdir(previous)
        # A signal is a failure too, and a unit with one action wrote its own errors.
        self.assertEqual(action.CompileFailed(top, -9, None).status, 1)
        action.CompileFailed(top, 101, None).report()

    def test_a_failed_compilation_raises_with_rustcs_status(self):
        top, base = self.units()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            (directory / "out").mkdir()
            payload = {"unit": top, "source": str(directory / "source"), "closure": [], "dependencies": [{"directory": str(directory / "base"), "spec": base}]}
            tools = {"rustc": "rustc", "cc": "cc"}
            failed = subprocess.CompletedProcess([], 101, stderr=b"error: no\n")
            with mock.patch.object(action.subprocess, "run", return_value=failed), self.assertRaises(action.CompileFailed) as raised:
                action.compile_unit(payload, directory / "out", {}, tools, directory, None, None, "metadata")
            # Held for report(), which decides whether this action shows them.
            self.assertEqual((raised.exception.status, raised.exception.diagnostics), (101, b"error: no\n"))
            with mock.patch.object(action.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), self.assertRaises(action.CompileFailed) as raised:
                action.compile_unit(payload, directory / "out", {}, tools, directory, None, None)
            self.assertIsNone(raised.exception.diagnostics)


class BuildScripts(unittest.TestCase):
    def test_a_build_script_gets_cargos_job_count_and_a_jobserver(self):
        script = """
import os
read = int(os.environ["CARGO_MAKEFLAGS"].rsplit("--jobserver-auth=", 1)[1].split(",")[0])
os.set_blocking(read, False)
try:
    tokens = len(os.read(read, 4096))
except BlockingIOError:
    tokens = 0
print(os.environ["NUM_JOBS"], tokens, os.environ["KEPT"])
"""
        for jobs in [1, 4]:
            result = action.run_with_jobserver([sys.executable, "-c", script], jobs, {"KEPT": "yes"}, text=True, stdout=subprocess.PIPE)
            # The script is one job itself; the tokens are the others.
            self.assertEqual(result.stdout.split(), [str(jobs), str(jobs - 1), "yes"])
        self.assertGreaterEqual(action.job_count(), 1)


class Workspace(unittest.TestCase):
    def test_a_build_script_finds_its_workspace_around_its_package(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            # Source trees as Buck stages them: directories, and links to files.
            checkout = directory / "checkout"
            for path in ["Cargo.lock", "helper/build.rs", "helper/src/lib.rs", "outside/src/lib.rs", "outside/assets/logo"]:
                (checkout / path).parent.mkdir(parents=True, exist_ok=True)
                (checkout / path).write_text(path)
            staged = {"": ["Cargo.lock"], "workspace/helper": ["helper/build.rs", "helper/src/lib.rs"], "outside": ["outside/src/lib.rs"]}
            sources = {}
            for relative, files in staged.items():
                sources[relative] = directory / "staged" / (relative or "root")
                for path in files:
                    link = sources[relative] / Path(path).relative_to(Path(path).parts[0] if relative else ".")
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(checkout / path)
            (sources["outside"] / "assets").symlink_to(checkout / "outside/assets", target_is_directory=True)
            workspace = directory / "output/workspace"
            action.assemble_workspace(workspace, sources)
            package = workspace / "workspace/helper"
            # `..` is resolved by the kernel: it must stay inside the assembled tree.
            self.assertFalse(package.is_symlink())
            self.assertEqual((package / "../../Cargo.lock").read_text(), "Cargo.lock")
            self.assertEqual((package / "src/../../../outside/src/lib.rs").read_text(), "outside/src/lib.rs")
            self.assertEqual((package / "build.rs").read_text(), "helper/build.rs")
            # A linked directory is linked, not copied.
            self.assertTrue((workspace / "outside/assets").is_symlink())
            self.assertEqual((workspace / "outside/assets/logo").read_text(), "outside/assets/logo")


class RuntimeEnvironment(unittest.TestCase):
    tools = {"cargo": "/toolchain/cargo", "rustlib": "/rustlib"}
    info = {"cargo_environment": {"CARGO_MANIFEST_DIR": "/package"}, "library_paths": ["/unit"]}

    def declared(self):
        return {"PATH": "/toolchain/bin", "RUSTC_BOOTSTRAP": "1"}

    def test_run_keeps_the_caller_environment(self):
        caller = {"RUST_LOG": "debug", "PATH": "/caller/bin", launcher.LIBRARY_PATH: "/caller/lib", "BUCK2_LOCAL_CAS_DIR": "cas",
                  "BUCK2_EXTRA_EXTERNAL_CONFIG": "config", "SHIKKA_RUN_CWD": "/caller"}
        env = launcher.runtime_environment(launcher.invoking_environment(caller, False, self.fail), self.tools, self.info, {})
        self.assertEqual(env, {"RUST_LOG": "debug", "PATH": "/caller/bin", "CARGO": "/toolchain/cargo", "CARGO_MANIFEST_DIR": "/package",
                               launcher.LIBRARY_PATH: "/unit:/rustlib:/caller/lib"})

    def test_tests_receive_the_caller_environment_from_the_snapshot(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as snapshot:
            json.dump({"DATABASE_URL": "postgres://test", "PATH": "/caller/bin"}, snapshot)
            snapshot.flush()
            base = launcher.invoking_environment({"SHIKKA_ENVIRONMENT": snapshot.name, "DAEMON_ONLY": "1"}, True, self.fail)
        self.assertEqual(base, {"DATABASE_URL": "postgres://test", "PATH": "/caller/bin"})

    def test_tests_without_a_snapshot_keep_the_declared_environment(self):
        base = launcher.invoking_environment({"DATABASE_URL": "postgres://daemon", "XDG_SESSION_TYPE": "wayland"}, True, self.declared)
        self.assertEqual(base, {"PATH": "/toolchain/bin", "RUSTC_BOOTSTRAP": "1", "XDG_SESSION_TYPE": "wayland"})

    def test_library_path_has_no_empty_entry(self):
        env = launcher.runtime_environment({}, self.tools, dict(self.info, library_paths=[]), {})
        self.assertEqual(env[launcher.LIBRARY_PATH], "/rustlib")


if __name__ == "__main__":
    unittest.main()
