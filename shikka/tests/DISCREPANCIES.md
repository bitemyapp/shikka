# Compatibility boundaries

Reviewed 2026-11-14. Cargo is the reference implementation, not a separate
written conformance standard. This suite covers a deliberately small native
build contract; it does not certify full Cargo compatibility.

| ID | Accepted boundary | Effect / affected checks |
| --- | --- | --- |
| DISC-001 | Native Linux/macOS, dev/release/test profiles | No cross compilation, custom profiles, doctests, benches or examples in the importer |
| DISC-002 | Explicit toolchain and generated graph | Manifest/config/app changes and toolchain files replaced in place require `generate`, which probes the toolchain again; unsupported implicit rustflags, runners, wrappers, environment and target configuration are rejected, as is an option given to a command that does not read it. A package's own compiler flags come from `[package.metadata.shikka] rustflags`, in place of a wrapper |
| DISC-003 | Private test HOME/temp; package source cwd | Tests receive the caller's environment but cannot depend on its session bus, home or temp directory; intentional-failure and test-path checks cover this contract |
| DISC-004 | Root-scoped Darwin actions and Linux test units | Embedded test paths remain valid after child chdir; ordinary Linux build action identities stay relocatable |
| DISC-005 | Linux ordinary builds use `/proc/self/cwd` | Applications embedding Cargo/build-script absolute paths and resolving them after changing cwd need explicit resource packaging; this is not a portable runtime resource API. `shikka run` starts a program in the caller's directory, so this includes one run from below the workspace root |
| DISC-006 | Optional cache-only NativeLink 1.8.3 | No remote worker deployment or offline binary bootstrap; cache-off requires no NativeLink |
| DISC-007 | One project root holds every local package | A path dependency outside the Cargo workspace needs `init --root DIR`; `init` refuses a root that cannot hold one before writing anything. Cargo and rustup still run in the workspace |
| DISC-008 | The graph is `cargo build --workspace` | Every member is a package Cargo was asked to build, so a member's dylib is not given `-C prefer-dynamic`, as it would be when built only as a dependency by `cargo build -p`. One that others link asks for it in `rustflags` |
| DISC-009 | A build script's inputs are its package, plus what its manifest declares | `build-script-reads-workspace` adds the workspace manifest, lockfile and local dependency sources. Other files outside the package are not there. On Linux a manifest directory compiled into a build script (`env!`) is `.`: right where the script runs, but not absolute |

The fixture is handwritten source, not generated output. `qualify.py` generates
its reference by running the installed Cargo with the same compiler, compares
stdout byte-for-byte, and checks test/compiler error exits. JSON observations
record commands, duration and status. `SHIKKA_TEST_LIGHT=1` uses the reviewed
`42:hello` output and avoids duplicate Cargo compilation on the laptop.

| Contract | Checks | Linux | macOS |
| --- | --- | --- | --- |
| Cargo feature resolution, proc macros, OUT_DIR and rustc-env | stdout comparison | differential | light fixture |
| Test cwd and embedded binary/manifest paths after chdir | fixture integration test | executed | executed |
| Failed compile/test status and incremental recovery | qualify.py | executed | executed |
| Configuration freshness / unsupported flags | qualify.py | executed | executed |
| Caller directory and environment for `run`; caller environment for tests | stdout comparison, fixture integration test | differential | light fixture |
| Toolchain file replaced in place; options a command does not read | qualify.py, test_frontend.py | executed | executed |
| Path dependency outside the Cargo workspace; refused `init` leaves nothing and names a root | qualify.py | executed | executed |
| Dylib built as a dependency and proc macro link std dynamically | stdout comparison | differential | light fixture |
| Build script: compiled-in manifest directory; workspace lockfile and local dependency source | fixture build scripts | executed | executed |
| Cargo's `prefer-dynamic` rule; package settings; workspace layout for a build script; Cargo and rustup run in the workspace | test_frontend.py | executed | executed |
| Service ownership, concurrent startup, mTLS, active-build stop guard, restart | test_service.py | real NativeLink | real NativeLink |

These are finite regression checks, not a percentage score against all Cargo
features. New compatibility claims require new reference cases here.
