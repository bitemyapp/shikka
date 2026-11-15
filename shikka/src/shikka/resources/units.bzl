"""One Buck action per Cargo unit; Cargo resolves, Buck schedules and caches.

With `[shikka] pipelining = true` an eligible library has a second action,
which produces its metadata alone. Libraries that depend on it compile against
that and start when it is written, not when the library is complete, as under
Cargo. Libraries that may invoke procedural macros keep one compilation.
"""

CargoUnitInfo = provider(fields = {
    "output": provider_field(Artifact),
    # What a library compiling against this unit reads: the metadata of a
    # pipelined library, the output of anything else.
    "interface": provider_field(Artifact),
    "pipelined": provider_field(bool),
    # A procedural macro is reachable through compiler dependencies, possibly
    # re-exported by a library. Build-script execution is a separate boundary.
    "proc_macros": provider_field(bool),
    # Every unit below this one, by label. A unit that links needs their
    # outputs; a library needs only their interfaces.
    "objects": provider_field(dict),
    "interfaces": provider_field(dict),
    "spec": provider_field(dict),
})

SourceInfo = provider(fields = {
    "path": provider_field(typing.Any),
    "inputs": provider_field(list),
})

def _source_impl(ctx):
    if ctx.attrs.store_path:
        # Nix store paths encode immutable source content. They are available in
        # the local sandbox and in the read-only worker store, not host caches.
        path = ctx.attrs.store_path
        outputs = []
    else:
        path = ctx.actions.symlinked_dir("source", ctx.attrs.files)
        outputs = [path]
        if ctx.attrs.prefix:
            path = path.project(ctx.attrs.prefix)
    return [DefaultInfo(default_outputs = outputs), SourceInfo(path = path, inputs = outputs)]

cargo_source = rule(
    impl = _source_impl,
    attrs = {
        "files": attrs.dict(attrs.string(), attrs.source(), default = {}),
        "store_path": attrs.string(default = ""),
        "prefix": attrs.string(default = ""),
    },
)

def _library(spec):
    # Cargo's rule for pipelining: a unit that produces only an rlib does not
    # read the objects of the units below it, and a library above it does not
    # read this one's.
    if spec["mode"] != "build" or "custom-build" in spec["target"]["kind"]:
        return False
    return all([kind in ["lib", "rlib"] for kind in spec["target"]["crate_types"]])

def _run(ctx, spec, manifest, closure, incremental, category, prefix, outputs):
    """One of a unit's actions. `prefix` names what only this action declares."""
    python = read_root_config("shikka", "python")
    environment = ctx.attrs._environment
    if not python or not environment:
        fail("Initialize this project with shikka init.")
    incremental_args = []
    if incremental:
        # Advisory compiler state is not part of CargoUnitInfo or its closures:
        # consumers depend only on the actual library/build-script outputs.
        state = ctx.actions.declare_output(prefix + "incremental", dir = True, has_content_based_path = False)
        incremental_args = ["--incremental-directory", state.as_output()]
    source_inputs = ctx.attrs.source[SourceInfo].inputs
    dep_files = {}
    dep_file_args = []
    if spec["mode"] != "run-custom-build" and source_inputs:
        source_tag = ctx.actions.artifact_tag()
        source_inputs = source_tag.tag_artifacts(cmd_args(source_inputs))
        dep_file = ctx.actions.declare_output(prefix + "rust.dependencies", has_content_based_path = not incremental)
        dep_file_args = ["--dep-file", source_tag.tag_artifacts(dep_file.as_output())]
        dep_files["rust"] = source_tag
    ctx.actions.run(
        cmd_args(
            python,
            ctx.attrs._runner,
            "--environment",
            environment,
            "--spec",
            manifest,
            outputs,
            dep_file_args,
            incremental_args,
            # A projected crate path omits declared sibling resources from the
            # remote input tree. Retain the complete, narrowly assembled tree.
            hidden = [source_inputs, closure.values()] + [source[SourceInfo].inputs for source in ctx.attrs.extra_sources.values()],
        ),
        category = category,
        identifier = spec["package"]["name"] + "-" + spec["target"]["name"],
        priority = ctx.attrs.priority,
        allow_cache_upload = True,
        dep_files = dep_files,
        allow_dep_file_cache_upload = True,
        no_outputs_cleanup = incremental,
        incremental_remote_outputs = incremental,
    )

def _unit_impl(ctx):
    spec = json.decode(ctx.attrs.spec)
    library = _library(spec)
    proc_macros = "proc-macro" in spec["target"]["crate_types"]
    objects = {}
    interfaces = {}
    dependencies = []
    for dep in ctx.attrs.deps:
        info = dep[CargoUnitInfo]
        if info.spec["mode"] != "run-custom-build":
            proc_macros = proc_macros or info.proc_macros
        label = str(dep.label.raw_target())
        objects.update(info.objects)
        objects[label] = info.output
        interfaces.update(info.interfaces)
        interfaces[label] = info.interface
        dependency = {"directory": info.interface if library else info.output, "spec": info.spec}
        if library and info.pipelined:
            dependency["metadata"] = True
        dependencies.append(dependency)
    # Expanding a proc macro twice may produce different metadata (for example
    # const-random). Cargo's graph cannot say whether a library re-exports a
    # macro, so include transitive compiler dependencies in this guard. A macro
    # used only to compile a build script cannot reach the library this way.
    pipelined = library and not proc_macros and read_root_config("shikka", "pipelining", "false") == "true"
    closure = interfaces if library else objects
    incremental = spec["mode"] != "run-custom-build" and spec["profile"]["incremental"]
    output = ctx.actions.declare_output("unit", dir = True, has_content_based_path = not incremental)
    payload = {
        "unit": spec,
        "source": ctx.attrs.source[SourceInfo].path,
        "dependencies": dependencies,
        "closure": closure.values(),
        "extra_sources": {key: value[SourceInfo].path for key, value in ctx.attrs.extra_sources.items()},
    }
    # Dependencies remain unconditional; only the compiler's source inputs may
    # be filtered by rustc's dep-info. Do not visit them again through the JSON.
    manifest = ctx.actions.write_json("action.json", payload, with_inputs = False)
    category = "cargo_" + spec["mode"].replace("-", "_")
    interface = output
    if pipelined:
        _run(ctx, spec, manifest, closure, incremental, category, "", ["--output", output.as_output(), "--pipelined", "objects"])
        # The same compilation without code generation. Its metadata is
        # identical, and done sooner; it costs the analysis a second time.
        interface = ctx.actions.declare_output("metadata", dir = True, has_content_based_path = not incremental)
        _run(ctx, spec, manifest, closure, incremental, "cargo_metadata", "metadata-", ["--output", interface.as_output(), "--pipelined", "metadata"])
    else:
        _run(ctx, spec, manifest, closure, incremental, category, "", ["--output", output.as_output()])
    executable = output.project(spec["filename"]) if spec["filename"] else None
    providers = [
        # A selected library must finish its dependencies' code generation,
        # even when nothing links it in this command. Keep these requirements
        # on the target, not the compiler action, so metadata can still unblock
        # compilation before the dependency's objects are ready.
        DefaultInfo(default_output = executable if executable else output, other_outputs = [output] + objects.values()),
        CargoUnitInfo(
            output = output,
            interface = interface,
            pipelined = pipelined,
            proc_macros = proc_macros,
            objects = objects,
            # Through a unit that linked, a library above it sees the objects
            # that unit needed: more than it reads, and all already built.
            interfaces = interfaces if library else objects,
            spec = spec,
        ),
    ]
    python = read_root_config("shikka", "python")
    runtime = read_root_config("shikka", "runtime", "")
    # Runtime metadata and CARGO_BIN_EXE_* reference other unit outputs. They
    # must be staged for remote tests, not merely present after local builds.
    run = cmd_args(
        python, ctx.attrs._runtime, "--action-module", ctx.attrs._runner,
        "--environment", ctx.attrs._environment, "--runtime", runtime, "--run", output,
        hidden = [closure.values(), ctx.attrs.source[SourceInfo].inputs] + [source[SourceInfo].inputs for source in ctx.attrs.extra_sources.values()],
    )
    if spec["mode"] == "test":
        providers.append(ExternalRunnerTestInfo(type = "rust", command = [cmd_args(run, "--test-run", "--")], env = {}, labels = [], contacts = [], run_from_project_root = True, use_project_relative_paths = True))
    elif "bin" in spec["target"]["crate_types"] and spec["mode"] == "build":
        providers.append(RunInfo(args = cmd_args(run, "--")))
    return providers

cargo_unit = rule(
    impl = _unit_impl,
    attrs = {
        "spec": attrs.string(),
        "_environment": attrs.default_only(attrs.source(default = "root//.shikka:toolchain.json")),
        "priority": attrs.int(default = 0),
        "source": attrs.dep(providers = [SourceInfo]),
        "deps": attrs.list(attrs.dep(providers = [CargoUnitInfo]), default = []),
        "extra_sources": attrs.dict(attrs.string(), attrs.dep(providers = [SourceInfo]), default = {}),
        "_runner": attrs.default_only(attrs.source(default = "shikka//:action.py")),
        "_runtime": attrs.default_only(attrs.source(default = "shikka//:run.py")),
    },
)
