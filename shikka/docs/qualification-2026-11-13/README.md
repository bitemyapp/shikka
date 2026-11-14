# Shikka extraction qualification — 2026-11-13

The standalone application builds and tests an unrelated Cargo repository,
ripgrep, with no Sekka checkout or Nix evaluation in ordinary build commands.
The optional managed cache works across separate Linux checkouts and service
restart. Mac checks use a small fixture and a disposable APFS image.

## Linux timing

Host: `ssh wx-workstation` (`theophrastus`), AMD Threadripper PRO 9985WX,
128 logical CPUs, Btrfs, Linux 7.3.1. Shikka uses 12 build threads here.
The application is installed by the root Nix flake, with Rust/Cargo 1.99.0 and
the pinned fork engine. NativeLink is 1.8.3. ripgrep commit:
`178cc027e283fd965cb72d0a7395b219eada170c`.

Wall-clock times include CLI, daemon/cache startup when needed, and the Linux
storage-sharing pass. Init/vendor/graph generation is outside build timing.
Unchanged/small/deep numbers are medians of three samples; other rows are single
runs. This is a short functional benchmark, not a controlled performance study.
Other workstation jobs were present; no Shikka engine compilation overlapped
these final measurements. Load averages at completion were 4.91 / 18.78 / 22.80.

| Case | Seconds | Actions |
| --- | ---: | --- |
| Cold build, empty private cache | 14.513 | 54 local |
| Unchanged | 0.130 | 0 |
| Leaf source change | 2.200 | 2 local |
| Deep dependency change | 4.867 | 6 local |
| Revert leaf change | 0.897 | 2 cached |
| Revert deep change | 1.759 | 6 cached |
| Fresh checkout, same cache | 1.796 | 54 cached, 0 local |
| Another fresh checkout after NativeLink restart | 2.508 | 54 cached, 0 local |

The leaf edit adds a documented, unused function to `crates/core/main.rs`.
The deep edit adds a public function to `crates/matcher/src/lib.rs`, changing its
crate metadata and invalidating downstream actions. Each repetition changes its
return value. Source files are restored in `finally`, and the real `rg --version`
is checked outside timing. See `../../tests/benchmark.py` and `linux-benchmark.json`.
The first harness attempt hit ripgrep's deny-missing-docs lint; the final probe
has documentation and all final benchmark builds succeeded.

## Storage evidence

On the cold build the Linux kernel sharing pass deduplicated **1,017 files,
401,330,280 bytes (382.74 MiB)** between action outputs and NativeLink, in 0.105 s.
This is a per-pass count, not the total unique disk footprint. Subsequent passes
recognize already shared extents. All final pass reports have zero errors.
One initial not-yet-visible blob is retried; later reports have no missing blobs.

The final pre-test inventory spans three checkouts, their private NativeLink CAS,
and the private Buck content store: 1,525,910,422 logical bytes over 3,224 files.
For uncompressed mapped ranges FIEMAP reports 460,128,256 bytes of references,
420,806,656 unique bytes, and **39,321,600 shared bytes**. Another 1,073,659,904
mapping bytes are excluded because of encoded/inline/unknown flags, primarily
Btrfs compression. These are explicitly **not** counted as unshared space.
An attempted `compsize` measurement requires unavailable elevated read access;
no total physical-footprint or overall deduplication percentage is claimed.
The later full test run adds artifacts and is outside this inventory.

On APFS, the real engine/NativeLink fixture uses a new 1 GiB sparse disk image
and a 64 MiB incompressible artifact. Allocation is measured on that isolated
volume, not by summing `st_blocks` (which double-counts clones):

| Operation | Additional physical bytes |
| --- | ---: |
| Cold output plus CAS | 67,592,192 |
| Fresh cached clone | 323,584 |
| Modify 1 MiB of a clone | 1,048,576 |

Distinct inodes, write isolation, concurrent restoration, upload permissions,
upload size limits, failed/cancelled producers, loss/restart recovery, corrupt
blob rejection and repair all pass. The image was detached afterward.

## Test scope and provenance

- Linux: root Nix engine build/check phase passes, including the 21 Btrfs local
  content-store tests and the configured digest, remote-client, output-ownership,
  configuration, scheduling and privacy regressions. `linux-engine-tests.log.gz`
  is the complete build/check log. XFS code is unchanged but was not requalified.
- Real ripgrep: **14 test binaries pass, zero failures**, built through the
  installed Shikka application with managed NativeLink; see
  `linux-ripgrep-tests.log.gz`. This command covers Cargo unit/integration tests,
  not doctests or optional-feature configurations.
- Frontend: `linux-fixture.json` records a Cargo-versus-Shikka output/test/error
  comparison; `mac-fixture.json` records the small compatibility version without
  duplicate Cargo compilation. Proc macros, features, build scripts, package cwd,
  embedded test paths, failed builds/tests, recovery and config rejection pass.
- Installed packages: the final Nix app restores/runs ripgrep with 54 cache
  hits. Its universal wheel installs in an independent Mac virtual environment,
  starts managed NativeLink and passes the tiny project’s run and two test
  binaries; see `*-installed-smoke.log`.
- Supervisor: two test cases cover unsafe lock rejection and real concurrent
  start/mTLS/lease/restart behavior on both platforms (`*-service.log`). The
  lifecycle case contains multiple independent assertions.
- Mac engine: all 10 APFS unit tests pass. The optional second-volume test returns
  early without a second volume in this run; this is not cross-volume coverage.
  A cached engine rebuild used two jobs. The APFS hardlink fix is also backported
  to Sekka commit `8028d343` in PR 334.

Upstream engine base and the 21 original patch hashes are recorded in
`../../engine-origin.json`. Core extraction commit: `ddae5caa65ef247352907f14614e2198483c6ad2`.
The final packaging follow-up filters bytecode from the Python source closure
and removes a trailing blank line; benchmark action code is unchanged.
Linux engine derivation: `/nix/store/7brhl54accnsgr4yp8kgpmcmjf2amfqj-shikka-buck2-2026-10-15-local-content-store.drv`.
The standalone NativeLink executable came from the official 1.8.3 Linux musl
archive with SHA256 `1a6596c96f059d4e99f7352164321e1f1cdff435275667ad8489e83f7e2977c5`.

The tested contract and known differences from Cargo are in
`../../tests/DISCREPANCIES.md`. This evidence does not qualify Nix-on-Darwin,
Intel macOS, remote execution workers, or full Cargo command compatibility.

Final application derivation: `/nix/store/n95was3i9qmwvll8zhxl88ds0jwx86xh-shikka-0.1.0.drv`.
Final installed app: `/nix/store/h4rz9gk70vsr4g55w8v5npkhj3q81dgr-shikka-0.1.0`.
Probe-owned cache services and build daemons were stopped.
