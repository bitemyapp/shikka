# Developing Shikka

Work on a `codex/` topic branch from main and submit it for review. A branch
lands on main squashed or rebased. There are no merge commits, ever: not on
main, and not to bring a branch up to date, which is a rebase.

Either way of landing gives the commits new names. Before a branch that
imports or changes the engine lands, tag its head `import/NAME`, where the
branch is `codex/NAME`, and push the tag: `shikka/engine-origin.json` and the
qualification reports cite individual commits, and the provenance of the
engine is checkable only while the import commit, whose tree is upstream plus
the recorded patches, stays in the repository. The first import is under
`import/standalone-cargo`. Never move or delete an `import/` tag.

Release publication is inhibited. Upstream workflows are kept
as reference in `.github/upstream-workflows`, outside GitHub's workflow discovery
path; repository Actions are also disabled. Do not enable releases implicitly.
The Nix binary cache below is the one thing published, and only when someone
runs its command: no workflow does.

## Source map

- `app/`, `prelude/`, `gazebo/`, etc.: the actual Buck2 fork, retaining upstream history.
- `shikka/src/shikka`: standalone Python 3.11+ Cargo frontend and managed cache supervisor.
- `shikka/src/shikka/resources`: bundled Buck rules, compiler and test runners.
- `nix/engine.nix`, root flake: pinned engine compiler, regression checks, narrow frontend packaging.
- `nix/cache`: the Nix binary cache's settings, its publisher and the publisher's tests.
- `shikka/engine-origin.json`: source Sekka commit, upstream base and hashes of all 21 imported patches.
- `shikka/tests`: executable compatibility and service regressions; intentionally small contract.

NativeLink remains a separate optional process. The engine communicates using
REAPI and has filesystem-specific integration for storage sharing. The frontend
owns graph generation, explicit toolchains and service lifecycle. It does not
fork a second Cargo dependency resolver or embed the NativeLink server in Buck2.

## Installation

`nix build .#shikka --no-update-lock-file` builds the executable with the pinned
Buck2 fork and native Rust/C toolchain. `nix build .#buck2` builds just the engine.
Outputs are defined for x86_64 Linux and Apple-silicon macOS, not Intel macOS,
which the pinned Nixpkgs has dropped; the extracted Nix
package has been exercised on x86_64 Linux, not Nix-on-Darwin.

The Nix package's toolchain is a default. It has a compiler and a C toolchain
and no libraries, which is enough for crates that need none. A project whose
build scripts find C libraries with pkg-config, generate bindings or run other
tools sets `SHIKKA_TOOLCHAIN` to a JSON file of its own before `init`.
Lower-case keys name tools: `rustc`, `cargo`, `cc`, `cxx`, `ar`, and optionally
`mold`, which then links the dev profile. Upper-case keys become the
environment of every compilation and build script, such as `PKG_CONFIG_PATH`
and `LIBCLANG_PATH`. `BUILD_PATH` is their PATH, the `-L` directories in
`NIX_LDFLAGS` are given to rustc, and `LIBRARY_PATH` is where build scripts
and tests find shared libraries. `SHIKKA_DEFAULT_BUCK2` and
`SHIKKA_DEFAULT_CARGO` choose another engine or Cargo the same way.

Without Nix, install `shikka/` into a Python virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install ./shikka
# Build the fork using rust-toolchain / engine.nix's flags and prerequisites.
# In a Cargo project with a lockfile and installed Rust + C toolchain:
/path/to/.venv/bin/shikka --buck2 /path/to/buck2 init
```

The CLI has no Python runtime dependencies. Init/generate uses Cargo metadata,
`cargo vendor --locked`, and the unstable Cargo unit-graph interface with the
chosen rustc. Ordinary builds/tests/runs do not invoke Cargo or Nix.

`init` records the chosen tools in `.shikka/project.json`, as absolute paths or
as a deferral to the packaged toolchain, with the PATH that actions run with.
`init` and every `generate` resolve those choices and probe their versions into
`.shikka/toolchain.json`, which is part of every action's identity. A rustup
proxy is followed to the toolchain it selects for the project at that moment.
Builds do not probe. They compare the size and modification time of the recorded
tool files and, when one was replaced in place as rustup and system packages
do, stop and ask for `generate`. This notices a replaced file, not a changed
library behind an unchanged one. Nix store paths are immutable and never
trigger it. A tool found in the Nix store is recorded at its store path under
the name it was asked for by: rustc keeps its own default linker when the C
compiler is called `cc`, as it is under Cargo, and drops it for `gcc`. To choose
other tools or another action PATH, edit `project.json` and run `generate`.

Global options precede the command. `--profile release`, `-p PACKAGE`, `--bin
NAME`, `-j N` and `--buck-arg=FLAG` select and configure builds. `--features`,
`--no-default-features`, `--manifest-path` and the tool paths are recorded by
`init`; change them in `project.json` and run `generate`. An option given to a
command that does not read it is an error, and `shikka test` always uses
Cargo's test profile. `shikka targets` prints labels, `shikka outputs` prints
output locations, and `shikka check` verifies the graph and generated file
fingerprints.

Buck builds one tree, so the project root must contain every local package.
By default it is the Cargo workspace. When path dependencies lie outside the
workspace, `init` stops before writing anything and names a directory that
holds them; pass it as `--root DIR`. Cargo and rustup still run in the
workspace, where they find its `.cargo/config.toml` and `rust-toolchain.toml`,
and commands work from anywhere below the root. An `init` that fails can be
run again.

Two things Cargo's graph cannot say are read from a local package's manifest:

```toml
[package.metadata.shikka]
rustflags = ["-C", "prefer-dynamic"]
build-script-reads-workspace = true
```

`rustflags` are extra rustc arguments for that package's targets: what a
`RUSTC_WRAPPER` matching one crate does under Cargo, which Shikka refuses. The
package's build script sees them in `CARGO_ENCODED_RUSTFLAGS`; its compilation
does not get them. Shikka follows Cargo's own rule for `-C prefer-dynamic`:
proc macros and dylibs built only as dependencies get it. The graph is
`cargo build --workspace`, in which no member is only a dependency, so a
member's dylib that other crates link has to ask for it here, as it would need
a wrapper under Cargo.

A build script's inputs are its own package. `build-script-reads-workspace`
adds the workspace's `Cargo.toml` and `Cargo.lock` and the sources of the
package's local dependencies, laid out around the package as they are in the
checkout, and reruns the script when any of them changes. An unknown key is an
error, and a registry package's settings are ignored.

`shikka run` starts the program as Cargo does: in the caller's directory, with
the caller's environment plus the package's `CARGO_*` variables and library
path. `shikka test` gives tests the caller's environment too, apart from a
private HOME, TMPDIR, XDG_RUNTIME_DIR and CARGO_TARGET_DIR and unreachable
D-Bus addresses. Buck2's daemon starts tests and does not have that
environment, so the CLI passes it in a mode-0600 file under `buck-out/shikka`
and removes the file when the command ends. Tests started by plain `buck2 test`
get only the toolchain's declared environment.

Project state lives in `.shikka/`, and generated BUCK/.buckconfig files are
excluded through Git's local info/exclude. The importer fails if it would replace
an existing unowned file. Cargo workspaces stay separate. Shikka writes nothing
to a project's Cargo manifests; it reads `[package.metadata.shikka]` where the
project has added it.

## Scheduling

Each Cargo unit is one Buck2 action, and Buck2 runs as many at once as there
are build threads (`-j`, by default the CPUs). Two things Cargo does inside
that picture are done here as well.

A build script gets `NUM_JOBS` and a GNU make jobserver in `CARGO_MAKEFLAGS`,
as Cargo gives it; the `cc` and `cmake` crates and `make` compile in parallel
through them and serially without. The count is the CPUs the action may use.
The jobserver is that script's own, an inherited pipe with one token for each
job beyond the script itself, so scripts running at once do not share a
budget as they do under Cargo. Neither value is part of an action's identity.

Pipelining is the other. A library that depends on a library needs only its
metadata (`.rmeta`) to compile; the objects are needed by what links: a
program, a test, a proc macro, a build script, a dylib. Cargo starts the
library above as soon as rustc reports the metadata written, while the same
process goes on to generate code. A Buck2 action has no early output, so with
pipelining a library has two actions. `cargo_metadata` is the compilation
with `-Z no-codegen`, and leaves the metadata alone; the libraries above read
that. `cargo_build` is the complete compilation, whose library is what gets
linked. Which units are libraries in this sense is Cargo's own rule, written
down in `_library` in `units.bzl`.

Selecting a library still builds the objects of all its dependencies. These
are required outputs of the selected target, rather than inputs of its
compiler action: analysis can start early, but a build cannot report success
while a dependency's code generation is missing or has failed.

The price is each library's analysis run twice. That is idle time put to use
where the build cannot fill the machine, and a slower build where it can, so
`--pipelining auto`, the default, turns it on where the machine has 64 or
more CPUs. `--pipelining on` and `off` choose, for one command or, given to
`init`, as the project's choice in `project.json`. The number is the
machine's and not `-j`, so that a command without `-j` builds the same graph
as one with it. Without pipelining a library is one action, as before, and
nothing above is different. An action names the directories it
reads, so a library with library dependencies has a different identity under
each choice, and machines that choose differently share cached results only
for the rest.

Two things keep the pair of actions sound. They ask rustc for the same kinds
of output (`--emit=dep-info,metadata,link`), because the kinds are part of the
crate hash that dependents record: a library compiled against metadata from
one set of kinds does not link objects from another (E0460). And the metadata
`-Z no-codegen` writes is byte for byte what the complete compilation leaves
beside its library, which `qualify.py` checks on the fixture. A compiler that
broke either would fail there, or at the first link, not silently.

Measured on 2026-11-15 on a 64-core, 128-thread machine that another job
kept at a load of 7 to 35, building the 709 units of a 33-program workspace
in the dev profile, each figure a clean build in seconds:

| | Seconds |
|---|---|
| Cargo | 48.9, 49.3 |
| Before either change | 70.8, 70.8 |
| Build scripts given jobs, no pipelining | 58.0, 59.2 |
| Build scripts given jobs, pipelining | 57.2, 58.0, 58.2, 70.9 |

The first change is all of the difference that can be told from the noise
there: one build script, a CMake build on the longest chain, went from 41 to
48 seconds to 28 to 32. Pipelining does shorten the longest chain, to 54.8
seconds from 61.9 in the one pair of runs where both were recorded, and its
metadata actions added 17% to the compiler's CPU time. With the CPUs limited
it was 57 to 58 seconds against 59 to 60 on 64 cores, 83 against 74 on 32
and 141 against 122 on 16, which is where the default comes from. An edit to
a library whose dependents are programs is not faster with it: programs wait
for objects either way.

What remains between these figures and Cargo's is mostly not the compiler,
which takes no longer per unit here. It is what Buck2 does around each
action, and most of that is the local content store making each new output
durable before its action completes: with the store off the same clean build
was 8 to 9 seconds shorter, and an edit to a library that rebuilds 15
packages took 5.4 to 5.6 seconds instead of 6.8 to 7.5 (Cargo: 4.6 to 4.8).

Both actions run the analysis, so both fail when a library has errors. The
first to fail shows rustc's output and leaves a marker under
`buck-out/shikka/reported`, named for the unit and for the build Buck2 says
the action belongs to; the other says the errors are in the library's other
failed action. An action that cannot tell shows them. A failed compilation
exits with rustc's status and nothing more.

## Cache and filesystem contract

`--cache off` is the default. `--cache managed` uses a per-user NativeLink 1.8.3
service under `$XDG_CACHE_HOME/shikka/service-v1` (or `~/.cache`). Pass
`--state-dir PATH` to isolate it. The default CAS budget is 10 GiB, with a separate
action cache capped at 256 MiB. `--cache-bytes` changes the budget after stopping
the old service. OpenSSL is required to initialize mutual-TLS credentials; the
Nix wrapper supplies it. NativeLink's pinned filesystem layout is the reason
managed mode checks its version. The user supplies NativeLink separately under
its own license.

Private 0700 state and 0600 credentials are required. A supervisor and its child
hold the ownership lock; a killed supervisor cannot silently start a second CAS
writer. An orphan is reported for operator inspection rather than killing an
unverified PID. Stop it only after checking the live process and state path, then
restart normally. NativeLink owns all CAS writes and eviction. APFS never mutates
published CAS blobs; Linux dedupe uses verified extent sharing. `--no-local-cas`
uses normal cache transport without the local sharing shortcut.

Managed cache is local compilation plus remote-cache semantics, not a remote
executor. Remote mode accepts a separate private Buck configuration and defaults
to read-only. Credentials are never committed or placed in CLI arguments.

Linux test-unit identities include their checkout root. Darwin scopes all actions
to its root until embedded Rust paths have a portable relocation contract.
A Linux build script is compiled with `CARGO_MANIFEST_DIR=.`: it runs in its
package directory, where the `/proc/self/cwd` path other units get names
nothing, and `.` is the same in every checkout.
Btrfs/XFS storage code is imported without behavioral changes. The APFS follow-up
detaches rustc's hardlinked incremental aliases before capturing publication
guards; this avoids false concurrent-mutation failures from sibling ctime updates.

## Nix binary cache

`https://shikka-cache.sekka.computer` serves the Nix package, so that
`nix build .#shikka` downloads the engine instead of compiling and testing it.
It is the Cloudflare R2 bucket `shikka-cache` behind a custom domain, and its
paths are signed with `shikka-cache.sekka.computer-1`;
`nix/cache/settings.nix` names all three. It has nothing to do with NativeLink
or the cache of build actions described above.

It holds only what cache.nixos.org lacks of the package's runtime closure. On
x86_64 Linux that is three paths of 119: the engine, the frontend and the
default toolchain file, 43 MB compressed. The engine's source is the
repository without `shikka/`, `nix/`, `.github/`, the flake and its lock, and
`README.md`, `DEVELOPERS.md` and `AGENTS.md` (`nix/engine.nix`), so a commit
that touches only those keeps the engine's store path and publishes no new
engine. Any other file, `CHANGELOG.md` and `docs/` included, is part of the
engine's source. Build inputs such as the pinned
nightly compiler are not published: changing the engine still means building
it.

The flake's `nixConfig` names the cache and its public key. Nix applies that
only when this flake is the one being built, asks before accepting it unless
given `--accept-flake-config`, and takes the substituter only from a user its
daemon trusts or when the machine's `trusted-substituters` lists it. A flake
that takes Shikka as an input does not inherit the setting. Such a project
names the cache in its own configuration:

```
extra-substituters = https://shikka-cache.sekka.computer
extra-trusted-public-keys = shikka-cache.sekka.computer-1:Ma5/jNK/TXzXeNjz9UkjYKVU7vW9HyyBEkAa82+vAvk=
```

It gets the published store paths as long as it leaves Shikka's `nixpkgs` and
`rust-overlay` inputs alone. Making them follow its own gives another engine,
which no cache has.

`nix run .#push-cache` publishes, for the system it runs on:

1. It builds `.#shikka` and uploads `nix-cache-info`, without which Nix does
   not take a bucket for a cache: a new bucket needs no other preparation.
2. `nix/cache/prepare_cache.py stage` asks cache.nixos.org and the cache
   itself which paths of the closure they serve, and writes signed archives
   of the rest. A path already published stays as it is, even if this machine
   built it differently; one whose archive is missing or truncated is
   published again.
3. The archives are uploaded, then their metadata.
4. `prepare_cache.py check` asks the caches, not the local store, what a
   machine with an empty store would get. Every path of the closure must be
   served with an archive of the advertised length, and Nix downloads the
   published ones to verify their contents and signatures.

Before any of that it stops unless the cache it reads and the bucket it
writes are both Shikka's or both a trial's, and, for Shikka's, unless the
signing key is the one whose public half is in `settings.nix`.

Run it once on each platform. Only x86_64 Linux has been built and published;
the macOS packages have not been built at all.

It needs two credentials, neither of which belongs in the repository:

- The signing key,
  `~/.config/shikka-cache/shikka-cache.sekka.computer-1.sec`
  (`SHIKKA_CACHE_CONFIG` overrides the directory). Keep a backup: replacing it
  means a new key name here and in `settings.nix`, and everyone who trusted
  the old key trusting the new one.
- An R2 API token with Object Read & Write on `shikka-cache` only, stored as
  the AWS profile `shikka-cache` (region `auto`):

  ```sh
  nix run nixpkgs#awscli2 -- configure --profile shikka-cache
  ```

  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the environment take its
  place. From the token's value alone, as Cloudflare shows it for its own
  API, the access key ID is the token's ID and the secret key is the SHA-256
  of the value in hexadecimal.

`nix build .#checks.x86_64-linux.cache-publication` runs the publisher's
tests, which make real stores, keys and caches in the build's own directory
and use no network. `checks.x86_64-linux.cache-push-guards` runs the command
itself up to its build, with each combination it must refuse. To try the
whole command, point it at another cache with `SHIKKA_CACHE_URL` and
`SHIKKA_CACHE_BUCKET` together, and `SHIKKA_CACHE_ENDPOINT` and
`SHIKKA_CACHE_PROFILE` for another account. The trial bucket needs another
name than Shikka's. `rclone serve s3` over a directory, read back over HTTP
or as a `file://` cache, is enough.

Nothing removes old paths from the bucket. Do not use R2 lifecycle rules for
that: a path that has not changed keeps its first upload date while still in
use.

## Validation

On `ssh wx-workstation`, run the engine's Nix build/check phase, then:

```sh
export PYTHONPATH="$PWD/shikka/src"
export SHIKKA_DEFAULT_BUCK2=/path/to/this/forks/buck2
export SHIKKA_TEST_NATIVELINK=/path/to/nativelink-1.8.3
python3 -m unittest discover -s shikka/tests
python3 shikka/tests/qualify.py > qualification.json
```

The differential fixture uses the installed Cargo/Rust pair as its reference;
ensure it is the same toolchain passed to init. On the laptop use
`SHIKKA_TEST_LIGHT=1` and the cached engine. The harness then passes `-j 2` and
avoids compiling the reference twice; Shikka itself leaves Buck2's thread count
alone unless `-j` is given. `test_frontend.py` needs no engine. Test logs distinguish engine checks, frontend
checks, real external-repository tests, and storage evidence. No ISO or host disk
mutation is involved. Do not turn these checks into claims of full Cargo or XFS
qualification when that platform was not exercised.
