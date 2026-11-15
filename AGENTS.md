# Working on Shikka

Read DEVELOPERS.md for architecture, commands, compatibility boundaries and
validation. Work on a topic branch from main. Keep release publication inhibited.
Preserve the upstream engine history and engine/frontend license notices.

Heavy builds and benchmarks run on `ssh wx-workstation`; keep Mac checks small.
Keep Linux Btrfs/XFS storage behavior separate from Darwin APFS code. Never mutate
published NativeLink blobs on APFS or test storage operations on host block
devices. Use disposable directories and filesystems for fault injection.

Cargo owns dependency/feature/profile resolution. Do not maintain a second
resolver. Reject unsupported Cargo settings instead of silently ignoring them.
Keep optional NativeLink separate, explicitly supplied and properly licensed.
