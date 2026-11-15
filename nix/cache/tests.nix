# The publisher's tests: real Nix stores, keys and caches in the build's own
# temporary directory, with no network and no credentials.
{ pkgs }:
pkgs.runCommand "shikka-cache-publication-tests"
  {
    nativeBuildInputs = [
      pkgs.nix
      pkgs.python3
      pkgs.zstd
    ];
    # The builder of the fixtures' derivations, which run in a store of
    # their own: static, so it needs nothing registered there.
    TEST_SHELL = "${pkgs.pkgsStatic.busybox}/bin/busybox";
  }
  ''
    mkdir cache
    cp ${./prepare_cache.py} cache/prepare_cache.py
    cp ${./test_prepare_cache.py} cache/test_prepare_cache.py
    python3 -m unittest discover -s cache -p test_prepare_cache.py -v
    touch $out
  ''
