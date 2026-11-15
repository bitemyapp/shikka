# `nix run .#push-cache [FLAKE]`: builds Shikka for the system it runs on and
# uploads what cache.nixos.org lacks of it to Shikka's binary cache
# (settings.nix). DEVELOPERS.md describes the key and credentials it expects.
# Nothing runs it automatically.
{ pkgs, settings }:
pkgs.writeShellApplication {
  name = "shikka-push-cache";
  # Building and signing use the caller's own Nix and daemon.
  runtimeInputs = [
    pkgs.awscli2
    pkgs.python3
    pkgs.zstd
  ];
  text = ''
    flake=''${1:-.}
    config=''${SHIKKA_CACHE_CONFIG:-''${XDG_CONFIG_HOME:-$HOME/.config}/shikka-cache}
    key=$config/${settings.keyName}.sec
    # SHIKKA_CACHE_URL is where the bucket is read from and
    # SHIKKA_CACHE_BUCKET where it is written. A trial cache sets both.
    url=''${SHIKKA_CACHE_URL:-${settings.url}}
    url=''${url%/}
    endpoint=''${SHIKKA_CACHE_ENDPOINT:-${settings.endpoint}}
    bucket=''${SHIKKA_CACHE_BUCKET:-${settings.bucket}}
    profile=''${SHIKKA_CACHE_PROFILE:-shikka-cache}
    # Reading one cache and writing another publishes nothing useful, or the
    # wrong thing: paths the bucket lacks look published, or a trial's
    # paths land in Shikka's bucket. A bucket of that name is Shikka's
    # whatever endpoint it is reached by.
    reads=trial
    writes=trial
    if [[ $url == '${settings.url}' ]]; then reads=shikka; fi
    if [[ $bucket == '${settings.bucket}' ]]; then writes=shikka; fi
    if [[ $reads != "$writes" ]]; then
      echo "shikka-push-cache: $url and the bucket $bucket are not the same cache;" \
        "set both SHIKKA_CACHE_URL and SHIKKA_CACHE_BUCKET for a trial, neither to publish" >&2
      exit 1
    fi
    if [[ ! -r $key ]]; then
      echo "shikka-push-cache: signing key $key is missing" >&2
      exit 1
    fi
    # A path published under another key is one nobody trusts, and it would
    # stay: published paths are not replaced. A trial cache may use any key.
    public=$(nix key convert-secret-to-public < "$key")
    if [[ $writes == shikka && $public != '${settings.publicKey}' ]]; then
      echo "shikka-push-cache: $key is not the key the flake advertises for ${settings.url}" >&2
      exit 1
    fi
    # The token is the AWS profile, or the environment's where that is set.
    if [[ -n ''${AWS_ACCESS_KEY_ID:-} ]]; then
      s3() { aws --region auto --endpoint-url "$endpoint" s3 "$@"; }
    else
      s3() { aws --profile "$profile" --region auto --endpoint-url "$endpoint" s3 "$@"; }
    fi

    # The package's closure holds the engine and the default toolchain file.
    # Keep a failed build from being hidden by process substitution.
    build_outputs=$(nix build --no-update-lock-file --no-link --print-out-paths "$flake#shikka")
    mapfile -t outputs <<< "$build_outputs"

    # First what makes the bucket a binary cache: until it has this file Nix
    # refuses to ask it anything, the staging below included. The priority
    # puts it after cache.nixos.org (40).
    printf 'StoreDir: /nix/store\nWantMassQuery: 1\nPriority: 50\n' |
      s3 cp - "s3://$bucket/nix-cache-info" --content-type text/x-nix-cache-info

    # Query the caches before exporting anything, and stage only the paths
    # none of them serves, signed.
    stage=$(mktemp -d)
    trap 'rm -rf "$stage"' EXIT
    python3 ${./prepare_cache.py} stage --stage "$stage/upload" --key-file "$key" \
      --destination "$url" "''${outputs[@]}"

    # All archives before any metadata, so no client is told of an archive
    # that is not there yet.
    s3 cp --recursive --no-progress --content-type application/x-nix-nar \
      "$stage/upload/nar" "s3://$bucket/nar"
    s3 cp --recursive --no-progress --content-type text/x-nix-narinfo \
      --exclude '*' --include '*.narinfo' "$stage/upload" "s3://$bucket"
    # Then as a client sees it: every path of the closure from a cache, and
    # the published ones intact and signed with this key.
    python3 ${./prepare_cache.py} check --public-key "$public" \
      --destination "$url" "''${outputs[@]}"
  '';
  meta.description = "Upload Shikka's package to its binary cache";
}
