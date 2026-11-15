# Shikka's binary cache: a Cloudflare R2 bucket behind a custom domain.
# DEVELOPERS.md, "Binary cache", says what it holds and how it is published.
{
  # Where Nix reads it.
  url = "https://shikka-cache.sekka.computer";
  # The key its paths are signed with. The secret half is KEYNAME.sec in the
  # publisher's configuration directory, never in the repository.
  keyName = "shikka-cache.sekka.computer-1";
  # The public half, which flake.nix's nixConfig repeats: Nix reads that
  # without evaluating anything. The cache-settings check compares the two.
  publicKey = "shikka-cache.sekka.computer-1:Ma5/jNK/TXzXeNjz9UkjYKVU7vW9HyyBEkAa82+vAvk=";
  # Where the publisher writes it: the bucket and its account's S3 endpoint.
  bucket = "shikka-cache";
  endpoint = "https://c408b91e58b3aa56c4f71046fcd416b6.r2.cloudflarestorage.com";
}
