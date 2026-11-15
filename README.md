<img src="shikka/docs/shikka.svg" width="112" height="112" alt="Shikka's icon: a young buck peeking out of a wooden cargo crate">

# Shikka

Cargo projects, built by Buck2, with an optional shared NativeLink cache.

Shikka is Sekka's Buck2 fork extracted into a standalone application. The name
combines *shika* (鹿, deer—a buck) with Sekka's double **k**. Cargo still resolves
packages, features and profiles. Shikka imports that graph once; Buck2 handles
subsequent builds, incremental work, tests and artifact caching.

```sh
# From this checkout, on Linux with Nix:
nix build .#shikka
export PATH="$PWD/result/bin:$PATH"
cd /path/to/a/cargo/workspace
shikka init
shikka build
shikka test
shikka --bin my-app run -- --help
```

Compiling and testing the engine takes several minutes. On x86-64 Linux, Nix
can download it from Shikka's binary cache instead: answer yes when it asks
about the settings this flake adds, or pass `--accept-flake-config`. Nix only
takes that answer from a user it trusts;
[DEVELOPERS.md](DEVELOPERS.md#nix-binary-cache) has the cache's address and
key for other setups.

No NativeLink service is needed for local builds. To enable a private managed
cache, supply a NativeLink **1.8.3** executable:

```sh
shikka --cache managed --nativelink /path/to/nativelink build
shikka cache status
shikka cache stop
```

The service starts on demand and survives builds so other initialized projects
can reuse it. It listens on loopback with mutual TLS, keeps credentials in a
private user directory, and refuses to stop while builds hold activity leases.
`shikka --cache remote --config-file /path/to/private.buckconfig build` connects
to an existing cache. Remote cache uploads require `--cache-write`.

On Btrfs and reflink-capable XFS, artifacts can share physical storage while
Buck2 and NativeLink retain their own files and eviction policies. APFS uses
native clones inside Buck2's output-publication lifecycle. Filesystems without
these capabilities fall back to ordinary storage and cache transport.

This is an early native Rust importer, not a replacement for every Cargo
command. It supports dev/release builds, unit/integration tests, build scripts,
proc macros and resolved features. Run `shikka generate` after manifest,
configuration, toolchain or Shikka changes. Cross compilation, doctests,
custom profiles, examples/benches and remote worker deployment are outside the
current importer. Existing Buck projects are not overwritten. Unsupported
implicit Cargo compiler/target/environment settings are rejected.

Linux ordinary build actions can be reused across checkouts. Test actions and
Darwin actions are scoped to the checkout because Rust can embed absolute
runtime paths. Programs that use embedded Cargo build paths as runtime resource
locations need explicit resource packaging. See [compatibility boundaries](shikka/tests/DISCREPANCIES.md).

The engine retains Buck2's MIT/Apache-2.0 licenses; the frontend is Apache-2.0
with Cargo's MIT-licensed LTO planner attribution. NativeLink is separately
licensed: version 1.8.3 uses FSL-1.1-Apache-2.0 and has additional use conditions.
It is optional and is not bundled or relicensed by Shikka.

[Development and installation](DEVELOPERS.md) ·
[Upstream Buck2](https://github.com/facebook/buck2) ·
[NativeLink license](https://github.com/TraceMachina/nativelink/blob/v1.8.3/LICENSE)
