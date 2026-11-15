{ pkgs, rustOverlay }:
let
  rust = (pkgs.extend rustOverlay).rust-bin.nightly."2026-07-05".minimal;
  platform = pkgs.makeRustPlatform { cargo = rust; rustc = rust; };
in
platform.buildRustPackage ({
  pname = "shikka-buck2";
  version = "2026-10-15-local-content-store";
  src = pkgs.lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let relative = pkgs.lib.removePrefix (toString ../. + "/") (toString path);
          first = builtins.head (pkgs.lib.splitString "/" relative);
      in !(builtins.elem first [ ".git" ".github" "shikka" "nix" "result" "target" "flake.nix" "flake.lock" "DEVELOPERS.md" "AGENTS.md" "README.md" ])
         && pkgs.lib.cleanSourceFilter path type;
  };
  cargoHash = "sha256-bQiNyy/k6DA+Wwt4kHJn13KMcmZ1ouos3Dwn/I+4Sn4=";
  nativeBuildInputs = [ pkgs.clang ]
    ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.mold ]
    ++ [ pkgs.pkg-config pkgs.git ];
  cargoBuildFlags = [ "--bin" "buck2" ];
  RUSTFLAGS = "--cfg tokio_unstable -C linker=clang"
    + pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux " -C link-arg=-fuse-ld=mold";
  CARGO_PROFILE_RELEASE_INCREMENTAL = "false";
  # jemalloc otherwise runs CPUID during configure and emits different pointer
  # layouts on 48-bit and 57-bit hosts. Use its portable x86-64 fallback.
  # Auth/config and download-integrity regressions run on every builder.
  # Reflink tests additionally run on Btrfs; ext4 builders explicitly report
  # that coverage limit. Real Aizome extent sharing is qualified separately.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    for filter in init::tests argv::tests legacy_configs::telemetry::tests legacy_configs::args::tests settings::args::tests; do
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_common -- "$filter"
    done
    for filter in immediate_config::tests common::tests; do
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_client_ctx -- "$filter"
    done
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_re_configuration -- privacy_tests
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_server_ctx -- ctx::telemetry_tests
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2 --lib -- tests::cli_
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p host_sharing --lib
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- local_content_store::download_tests
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- local_content_store::linux::tests::hint_
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- local_content_store::linux::tests::local_cas_
    for filter in local_content_store::linux::tests::store_rejects_aliased_index_files_without_changing_targets \
      output_pipeline::tests output_watcher::tests prepared_outputs::tests; do
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- "$filter"
    done
    cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p remote_execution -- client::tests
    for filter in re::error::tests re::remote_action_result::tests re::presence::tests entry::tests; do
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute -- "$filter"
    done
    if [ "$(uname -s)" = Darwin ]; then
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- materializers::apfs::tests
    elif [ "$(stat -f -c %T "$TMPDIR")" = btrfs ]; then
      cargo test --frozen --release --target ${pkgs.stdenv.hostPlatform.rust.rustcTarget} --jobs "$NIX_BUILD_CORES" -p buck2_execute_impl --lib -- local_content_store::linux::tests
    else
      echo "Reflink-store execution not checked: this builder is not on Btrfs."
    fi
    runHook postCheck
  '';
  meta = {
    description = "Buck2 with verified, bounded local reflink artifact materialization";
    homepage = "https://github.com/facebook/buck2";
    license = with pkgs.lib.licenses; [ mit asl20 ];
    mainProgram = "buck2";
    platforms = [ "x86_64-linux" ] ++ pkgs.lib.platforms.darwin;
  };
} // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isx86_64 {
  JEMALLOC_SYS_WITH_LG_VADDR = "57";
})
