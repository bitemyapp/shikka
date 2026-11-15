# What the publisher must refuse before it builds or uploads anything. `nix
# build` is replaced by a stand-in that stops the run there with status 42, so
# reaching it means every guard let the run through. Nothing is uploaded.
{ pkgs, settings, push }:
let
  nix = pkgs.writeShellScript "nix-until-build" ''
    if [[ $1 == build ]]; then
      echo "reached the build" >&2
      exit 42
    fi
    exec ${pkgs.nix}/bin/nix "$@"
  '';
in
pkgs.runCommand "shikka-cache-push-guard-tests"
  {
    nativeBuildInputs = [ pkgs.nix ];
    NIX_CONFIG = "experimental-features = nix-command flakes";
  }
  ''
    export HOME=$PWD/home
    mkdir -p home stand-in other/shikka-cache advertised
    ln -s ${nix} stand-in/nix
    export PATH=$PWD/stand-in:$PATH
    nix key generate-secret --key-name ${settings.keyName} > other/shikka-cache/${settings.keyName}.sec

    # run STATUS MESSAGE [VARIABLE=VALUE]...: the command with another key
    # than the advertised one, expecting that status and that message.
    run() {
      local status=$1 message=$2 actual=0
      shift 2
      env XDG_CONFIG_HOME="$PWD/other" "$@" ${push}/bin/shikka-push-cache . 2> output || actual=$?
      if [[ $actual != "$status" ]] || ! grep -q -- "$message" output; then
        echo "with [$*]: status $actual, wanted $status and '$message'; it said:" >&2
        cat output >&2
        exit 1
      fi
    }

    run 1 "is not the key the flake advertises"
    run 1 "is not the key the flake advertises" SHIKKA_CACHE_URL=${settings.url}/
    run 1 "is not the key the flake advertises" SHIKKA_CACHE_ENDPOINT=http://127.0.0.1:1
    run 1 "are not the same cache" SHIKKA_CACHE_URL=http://127.0.0.1:1
    run 1 "are not the same cache" SHIKKA_CACHE_BUCKET=trial
    run 1 "are not the same cache" SHIKKA_CACHE_URL=${settings.url} SHIKKA_CACHE_BUCKET=trial
    run 1 "signing key .* is missing" XDG_CONFIG_HOME="$PWD/absent"
    # A trial may use any key, and gets as far as the build.
    run 42 "reached the build" SHIKKA_CACHE_URL=http://127.0.0.1:1 SHIKKA_CACHE_BUCKET=trial
    touch $out
  ''
