# Compatibility boundaries

Reviewed 2026-11-13. Cargo is the reference implementation, not a separate
written conformance standard. This suite covers a deliberately small native
build contract; it does not certify full Cargo compatibility.

| ID | Accepted boundary | Effect / affected checks |
| --- | --- | --- |
| DISC-001 | Native Linux/macOS, dev/release/test profiles | No cross compilation, custom profiles, doctests, benches or examples in the importer |
| DISC-002 | Explicit toolchain and generated graph | Manifest/config/app changes and toolchain files replaced in place require `generate`, which probes the toolchain again; unsupported implicit rustflags, runners, wrappers, environment and target configuration are rejected, as is an option given to a command that does not read it |
| DISC-003 | Private test HOME/temp; package source cwd | Tests receive the caller's environment but cannot depend on its session bus, home or temp directory; intentional-failure and test-path checks cover this contract |
| DISC-004 | Root-scoped Darwin actions and Linux test units | Embedded test paths remain valid after child chdir; ordinary Linux build action identities stay relocatable |
| DISC-005 | Linux ordinary builds use `/proc/self/cwd` | Applications embedding Cargo/build-script absolute paths and resolving them after changing cwd need explicit resource packaging; this is not a portable runtime resource API. `shikka run` starts a program in the caller's directory, so this includes one run from below the workspace root |
| DISC-006 | Optional cache-only NativeLink 1.8.3 | No remote worker deployment or offline binary bootstrap; cache-off requires no NativeLink |

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
| Service ownership, concurrent startup, mTLS, active-build stop guard, restart | test_service.py | real NativeLink | real NativeLink |

These are finite regression checks, not a percentage score against all Cargo
features. New compatibility claims require new reference cases here.
