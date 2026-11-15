{
  description = "Shikka: Cargo-authoritative builds with a storage-sharing Buck2 engine";
  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0.1";
  inputs.rust-overlay = {
    url = "github:oxalica/rust-overlay";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  # Shikka's binary cache (DEVELOPERS.md, "Nix binary cache"). Nix reads this
  # without evaluating the flake, so it repeats nix/cache/settings.nix; the
  # cache-settings check compares the two.
  nixConfig = {
    extra-substituters = [ "https://shikka-cache.sekka.computer" ];
    extra-trusted-public-keys = [ "shikka-cache.sekka.computer-1:Ma5/jNK/TXzXeNjz9UkjYKVU7vW9HyyBEkAa82+vAvk=" ];
  };
  outputs = { self, nixpkgs, rust-overlay }:
    let
      # Not x86_64-darwin: the pinned Nixpkgs no longer evaluates for it.
      systems = [ "x86_64-linux" "aarch64-darwin" ];
      forAll = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
      cache = import ./nix/cache/settings.nix;
      packagesFor = system:
        let
          pkgs = pkgsFor system;
          buck2 = import ./nix/engine.nix { inherit pkgs; rustOverlay = rust-overlay.overlays.default; };
          tools = pkgs.writeText "shikka-toolchain.json" (builtins.toJSON {
            rustc = "${pkgs.rustc}/bin/rustc";
            cargo = "${pkgs.cargo}/bin/cargo";
            cc = "${pkgs.stdenv.cc}/bin/cc";
            cxx = "${pkgs.stdenv.cc}/bin/c++";
            ar = "${pkgs.stdenv.cc.bintools}/bin/ar";
            BUILD_PATH = pkgs.lib.makeBinPath [ pkgs.stdenv.cc pkgs.stdenv.cc.bintools pkgs.coreutils pkgs.pkg-config pkgs.git pkgs.gnumake pkgs.cmake ];
          });
          app = pkgs.python3Packages.buildPythonApplication {
            pname = "shikka";
            version = "0.1.0";
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./shikka;
              fileset = pkgs.lib.fileset.unions [
                ./shikka/pyproject.toml ./shikka/LICENSE
                (pkgs.lib.fileset.fileFilter (file:
                  file.hasExt "py" || file.hasExt "bzl" ||
                  builtins.elem file.name [ ".buckconfig" "BUCK" "CARGO-LICENSE-MIT" ]) ./shikka/src)
              ];
            };
            build-system = [ pkgs.python3Packages.setuptools ];
            # Defaults, not settings: a project whose crates need C libraries
            # or other tools names its own toolchain in SHIKKA_TOOLCHAIN.
            makeWrapperArgs = [
              "--set-default" "SHIKKA_DEFAULT_BUCK2" "${buck2}/bin/buck2"
              "--set-default" "SHIKKA_TOOLCHAIN" "${tools}"
              "--set-default" "SHIKKA_DEFAULT_CARGO" "${pkgs.cargo}/bin/cargo"
              "--prefix" "PATH" ":" "${pkgs.lib.makeBinPath [ pkgs.openssl ]}"
            ];
            pythonImportsCheck = [ "shikka.cli" "shikka.generate" "shikka.service" ];
            meta = {
              description = "Cargo-authoritative Rust builds with optional NativeLink caching";
              license = pkgs.lib.licenses.asl20;
              mainProgram = "shikka";
              platforms = systems;
            };
          };
        in { inherit buck2; shikka = app; default = app; };
    in {
      packages = forAll packagesFor;
      apps = forAll (system: {
        default = { type = "app"; program = "${self.packages.${system}.shikka}/bin/shikka"; };
        # Publishes this system's package to the binary cache, by hand
        # (DEVELOPERS.md, "Nix binary cache").
        push-cache =
          let push = import ./nix/cache/push.nix { pkgs = pkgsFor system; settings = cache; };
          in { type = "app"; program = "${push}/bin/shikka-push-cache"; meta.description = push.meta.description; };
      });
      checks = forAll (system: {
        cache-settings =
          let advertised = (import ./flake.nix).nixConfig;
          in assert nixpkgs.lib.assertMsg
            (advertised.extra-substituters == [ cache.url ]
              && advertised.extra-trusted-public-keys == [ cache.publicKey ]
              && nixpkgs.lib.hasPrefix "${cache.keyName}:" cache.publicKey)
            "flake.nix's nixConfig and nix/cache/settings.nix name different caches or keys";
          (pkgsFor system).emptyFile;
      } // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
        # The publisher's tests build derivations in a store of their own,
        # which Nix can only do on Linux.
        cache-publication = import ./nix/cache/tests.nix { pkgs = pkgsFor system; };
      });
    };
}
