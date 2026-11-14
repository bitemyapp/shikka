{
  description = "Shikka: Cargo-authoritative builds with a storage-sharing Buck2 engine";
  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0.1";
  inputs.rust-overlay = {
    url = "github:oxalica/rust-overlay";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  outputs = { self, nixpkgs, rust-overlay }:
    let
      systems = [ "x86_64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = nixpkgs.lib.genAttrs systems;
      packagesFor = system:
        let
          pkgs = import nixpkgs { inherit system; };
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
            makeWrapperArgs = [
              "--set" "SHIKKA_DEFAULT_BUCK2" "${buck2}/bin/buck2"
              "--set" "SHIKKA_TOOLCHAIN" "${tools}"
              "--set" "SHIKKA_DEFAULT_CARGO" "${pkgs.cargo}/bin/cargo"
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
      });
    };
}
