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
repository without `shikka/`, `nix/`, the flake and the top-level documents
(`nix/engine.nix`), so a commit that touches only those keeps the engine's
store path and publishes no new engine. Build inputs such as the pinned
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

Before any of that it compares the signing key with the public key in
`settings.nix` and stops if they differ, unless `SHIKKA_CACHE_URL` names a
trial cache.

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
and use no network. To try the whole command, point it at another cache with
`SHIKKA_CACHE_URL` and `SHIKKA_CACHE_BUCKET` (and `SHIKKA_CACHE_ENDPOINT` and
`SHIKKA_CACHE_PROFILE` for another account). The two must name the same
cache, or paths the bucket lacks look published. `rclone serve s3` over a
directory, read back as a `file://` cache, is enough.

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
