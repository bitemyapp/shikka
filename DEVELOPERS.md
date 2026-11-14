# Developing Shikka

Work on a `codex/` topic branch from main and submit reviewed work before
squash-merging. Release publication is inhibited. Upstream workflows are kept
as reference in `.github/upstream-workflows`, outside GitHub's workflow discovery
path; repository Actions are also disabled. Do not enable releases implicitly.

## Source map

- `app/`, `prelude/`, `gazebo/`, etc.: the actual Buck2 fork, retaining upstream history.
- `shikka/src/shikka`: standalone Python 3.11+ Cargo frontend and managed cache supervisor.
- `shikka/src/shikka/resources`: bundled Buck rules, compiler and test runners.
- `nix/engine.nix`, root flake: pinned engine compiler, regression checks, narrow frontend packaging.
- `shikka/engine-origin.json`: source Sekka commit, upstream base and hashes of all 21 imported patches.
- `shikka/tests`: executable compatibility and service regressions; intentionally small contract.

NativeLink remains a separate optional process. The engine communicates using
REAPI and has filesystem-specific integration for storage sharing. The frontend
owns graph generation, explicit toolchains and service lifecycle. It does not
fork a second Cargo dependency resolver or embed the NativeLink server in Buck2.

## Installation

`nix build .#shikka --no-update-lock-file` builds the executable with the pinned
Buck2 fork and native Rust/C toolchain. `nix build .#buck2` builds just the engine.
Outputs are defined for x86_64 Linux and Apple/Intel macOS; the extracted Nix
package has been exercised on x86_64 Linux, not Nix-on-Darwin.

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
chosen rustc. Ordinary builds/tests/runs do not invoke Cargo or Nix. Toolchain
executables must remain installed; regenerate/reinitialize their configuration
when changing compiler versions. Nix store paths provide immutable toolchains;
mutable user toolchain installations require operator-managed regeneration.

Global options precede the command. `--profile release`, `-p PACKAGE`, `--bin
NAME`, `--features FEATURES` (at init), `--no-default-features`, `-j N`, and
`--buck-arg=FLAG` provide the supported selection/configuration interface.
`shikka targets` prints labels, `shikka outputs` prints output locations, and
`shikka check` verifies the graph and generated file fingerprints.

Project state lives in `.shikka/`, and generated BUCK/.buckconfig files are
excluded through Git's local info/exclude. The importer fails if it would replace
an existing unowned file. Cargo workspaces stay separate. Nothing is added to
another project's Cargo manifest.

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
Btrfs/XFS storage code is imported without behavioral changes. The APFS follow-up
detaches rustc's hardlinked incremental aliases before capturing publication
guards; this avoids false concurrent-mutation failures from sibling ctime updates.

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
`SHIKKA_TEST_LIGHT=1` and the cached engine, with two build threads. It avoids
compiling the reference twice. Test logs distinguish engine checks, frontend
checks, real external-repository tests, and storage evidence. No ISO or host disk
mutation is involved. Do not turn these checks into claims of full Cargo or XFS
qualification when that platform was not exercised.
