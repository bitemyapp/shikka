#!/usr/bin/env python3
"""Publish Shikka's packages to its binary cache without republishing Nixpkgs.

    prepare_cache.py stage --stage DIR --key-file KEY --destination URL OUTPUT...
    prepare_cache.py check --public-key KEY --destination URL OUTPUT...

`stage` writes signed, compressed archives for the paths of the outputs'
runtime closures that no cache serves yet. The publication wrapper
(push.nix) uploads the archives before their metadata. References an upstream
cache serves are not copied: clients take each path from whichever of their
substituters has it.

`check` asks the caches, not the local store, whether a machine with nothing
but them would download every path: it follows the references the caches
report, probes each archive, and has Nix verify the contents and signatures
of the paths the destination serves.

Derived from Sekka's scripts/prepare_cache.py and check_cache_coverage.py.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

# What a Nix installation asks unless told otherwise.
UPSTREAMS = ['https://cache.nixos.org']

# Each cache's current answer rather than Nix's remembered one, and a bounded
# wait, so a stalled cache fails the run instead of hanging it.
FRESH = ['--option', 'narinfo-cache-positive-ttl', '0',
         '--option', 'narinfo-cache-negative-ttl', '0',
         '--option', 'http-connections', '4']
TIMEOUT = 180


def command(*args, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True, check=True).stdout


def invalid_paths(cache, batch):
    """The paths of one batch the cache lacks, without downloading anything."""
    result = subprocess.run(
        ['nix-store', '--store', cache, *FRESH, '--check-validity', '--print-invalid', *batch],
        check=True, capture_output=True, text=True, timeout=TIMEOUT)
    missing = set(result.stdout.split())
    if not missing <= set(batch):
        raise ValueError(f'{cache}: unexpected missing paths')
    return missing


def cache_info(cache, paths):
    """Read fresh metadata through Nix without substituting any store paths."""
    # path-info can fail before emitting JSON for a missing path. Filter misses
    # first, and fail closed on network errors or malformed metadata.
    def batch_info(batch):
        missing = invalid_paths(cache, batch)
        present = sorted(set(batch) - missing)
        info = {}
        if present:
            result = subprocess.run(
                ['nix', 'path-info', '--store', cache, *FRESH, '--json', '--json-format', '1', '--stdin'],
                input='\n'.join(present) + '\n', check=True, capture_output=True, text=True, timeout=TIMEOUT)
            info = json.loads(result.stdout)
            if set(info) != set(present) or any(value is None for value in info.values()):
                raise ValueError(f'{cache}: unexpected store path metadata')
        return dict.fromkeys(missing) | info

    paths = sorted(paths)
    batches = [paths[i:i + 128] for i in range(0, len(paths), 128)]
    found = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in pool.map(batch_info, batches):
            found.update(batch)
    return found


def archive_error(cache, info):
    """A bounded archive availability probe, without downloading a whole NAR."""
    base = urlsplit(cache)._replace(query='', fragment='').geturl()
    url = urljoin(base.rstrip('/') + '/', info['url'])
    expected = info.get('downloadSize')
    try:
        parsed = urlsplit(url)
        if parsed.scheme == 'file':
            with Path(unquote(parsed.path)).open('rb') as stream:
                size = stream.seek(0, 2)
        elif parsed.scheme in ('https', 'http'):
            # Some binary caches reject generic Python clients. Identify this
            # Nix cache verifier explicitly, without pretending to be a browser.
            headers = {'User-Agent': 'Nix/shikka-cache'}
            try:
                response = urlopen(Request(url, method='HEAD', headers=headers), timeout=30)
            except HTTPError as error:
                error.close()
                if error.code not in (405, 501):
                    raise
                response = urlopen(Request(url, headers=headers | {'Range': 'bytes=0-0'}), timeout=30)
            with response:
                if response.status == 206:
                    size = int(response.headers['Content-Range'].rsplit('/', 1)[1])
                elif response.status == 200:
                    size = response.headers.get('Content-Length')
                    size = int(size) if size is not None else None
                else:
                    raise ValueError(f'unexpected HTTP status {response.status}')
        else:
            raise ValueError(f'unsupported archive URL scheme {parsed.scheme}')
        if expected is not None and size != expected:
            raise ValueError(f'archive length {size}, expected {expected}')
    except (OSError, URLError, ValueError, KeyError) as error:
        if isinstance(error, HTTPError):
            error.close()
        return f'{url}: {error}'
    return None


def archive_errors(cache, found):
    """Each path's archive problem, or None, for metadata one cache returned."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(found, pool.map(lambda path: archive_error(cache, found[path]), found)))


def missing_from(cache, paths):
    """Batch existence checks without downloading or recompressing store paths."""
    paths = sorted(paths)
    batches = [paths[i:i + 128] for i in range(0, len(paths), 128)]
    missing = set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for invalid in pool.map(partial(invalid_paths, cache), batches):
            missing.update(invalid)
    return missing


def unpublished(info, upstreams, destination):
    missing = set(info)
    for cache in upstreams:
        if missing:
            missing = missing_from(cache, missing)
    # A path the destination already describes is not compressed again. Do not
    # skip a dangling or truncated archive: repair it.
    existing = cache_info(destination, missing)
    candidates = {path: data for path, data in existing.items() if data is not None}
    for path, error in archive_errors(destination, candidates).items():
        if error is not None:
            print(f'Repairing unavailable archive for {path}: {error}', file=sys.stderr)
            continue
        cached = candidates[path]
        local = info[path]
        if cached['narHash'] != local['narHash'] or cached['narSize'] != local['narSize'] \
                or set(cached['references']) != set(local['references']):
            # The same derivation built differently, as one that is not
            # reproducible is on another machine. The published copy is
            # valid and signed; replacing it would only alternate the
            # cache between the two with every publishing machine.
            print(f'Keeping the published copy of {path}, which differs from the local build',
                  file=sys.stderr)
        missing.remove(path)
    return sorted(missing)


def sri_sha256(value):
    if not value.startswith('sha256-'):
        raise ValueError(f'Expected a SHA-256 SRI hash, got {value}')
    digest = base64.b64decode(value[len('sha256-'):], validate=True)
    if len(digest) != 32:
        raise ValueError('Invalid SHA-256 digest length')
    return digest


def export_path(path, info, stage):
    """Stream one canonical NAR into zstd; never export its cached references."""
    temporary = tempfile.NamedTemporaryFile(prefix='.nar-', dir=stage, delete=False)
    archive = Path(temporary.name)
    digest, size = hashlib.sha256(), 0
    try:
        with temporary, tempfile.TemporaryFile() as dump_error, tempfile.TemporaryFile() as zip_error:
            # Leaving the inner block closes this end of each pipe and waits:
            # an exporter still writing fails, and zstd sees its input end.
            with subprocess.Popen(['nix', 'store', 'dump-path', path],
                                  stdout=subprocess.PIPE, stderr=dump_error) as dump, \
                    subprocess.Popen(['zstd', '-q', '-T2', '-3', '-c'], stdin=subprocess.PIPE,
                                     stdout=temporary, stderr=zip_error) as compress:
                try:
                    while block := dump.stdout.read(1024 * 1024):
                        digest.update(block)
                        size += len(block)
                        compress.stdin.write(block)
                except BrokenPipeError:
                    pass  # zstd stopped early; its status and messages follow
                finally:
                    try:
                        compress.stdin.close()
                    except BrokenPipeError:
                        pass
            if dump.returncode or compress.returncode:
                messages = []
                for stream in (dump_error, zip_error):
                    stream.seek(0)
                    messages.append(stream.read().decode(errors='replace'))
                raise ValueError(f'NAR export failed for {path}: ' + ''.join(messages))
        if digest.digest() != sri_sha256(info['narHash']) or size != info['narSize']:
            raise ValueError(f'Exported NAR does not match registered contents: {path}')
        with archive.open('rb') as stream:
            file_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        url = f'nar/{file_hash}.nar.zst'
        file_size = archive.stat().st_size
        archive.rename(stage / url)
        fields = [
            f'StorePath: {path}', f'URL: {url}', 'Compression: zstd',
            f'FileHash: sha256:{file_hash}', f'FileSize: {file_size}',
            f'NarHash: sha256:{digest.hexdigest()}', f'NarSize: {size}',
            'References: ' + ' '.join(Path(ref).name for ref in sorted(info['references'])),
        ]
        if info.get('deriver'):
            fields.append('Deriver: ' + Path(info['deriver']).name)
        if info.get('ca'):
            # As nix copy writes it: a client may accept a content-addressed
            # path by its contents alone, without a signature.
            fields.append('CA: ' + info['ca'])
        # Nix canonicalizes the metadata and creates its own signature below.
        (stage / (Path(path).name.split('-', 1)[0] + '.narinfo')).write_text('\n'.join(fields) + '\n')
    finally:
        archive.unlink(missing_ok=True)


def prepare(outputs, stage, key, upstreams, destination, jobs=4):
    stage = Path(stage).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    if any(stage.iterdir()):
        raise ValueError('The staging directory must be empty')
    info = json.loads(command('nix', 'path-info', '--recursive', '--json', '--json-format', '1', *outputs))
    if not info or any(value is None for value in info.values()):
        raise ValueError('The publication roots must exist in the local store')
    paths = unpublished(info, upstreams, destination)
    (stage / 'nar').mkdir()
    # A failed export cancels the exports not started yet.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(lambda path: export_path(path, info[path], stage), paths))
    if paths:
        # Nix completes the stage as a binary cache (nix-cache-info) as it signs.
        command('nix', 'store', 'sign', '--store', stage.as_uri(), '--key-file', str(key),
                '--stdin', input='\n'.join(paths) + '\n')
    print(f'Staged {len(paths)} of {len(info)} paths; the others are already cached', flush=True)
    return paths


def check(outputs, upstreams, destination, public_key):
    """The paths the destination must serve for these outputs, verified there."""
    served, pending = {}, set(command('nix', 'path-info', *outputs).split())
    if not pending:
        raise ValueError('The outputs to check must exist in the local store')
    while pending:
        remaining, references, problems = set(pending), set(), {}
        # In the order a client asks them: an upstream's copy wins.
        for cache in [*upstreams, destination]:
            if not remaining:
                break
            found = {path: data for path, data in cache_info(cache, remaining).items() if data is not None}
            for path, error in archive_errors(cache, found).items():
                if error is not None:
                    problems.setdefault(path, []).append(error)
                    continue
                served[path] = cache
                remaining.remove(path)
                references.update(found[path]['references'])
        if remaining:
            raise ValueError('No cache serves:\n' + '\n'.join(
                path + ''.join(f'\n  {problem}' for problem in problems.get(path, []))
                for path in sorted(remaining)))
        pending = references - served.keys()
    own = sorted(path for path, cache in served.items() if cache == destination)
    if own:
        # Downloads each archive: the contents must match the signed metadata.
        command('nix', 'store', 'verify', '--store', destination, *FRESH, '--sigs-needed', '1',
                '--trusted-public-keys', public_key, *own)
    print(f'{len(served)} paths download: {len(own)} from {destination}, verified against '
          f'{public_key.split(":", 1)[0]}; the others from upstream', flush=True)
    return own


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('stage', 'check'):
        sub = commands.add_parser(name)
        sub.add_argument('--destination', required=True)
        sub.add_argument('--upstream', action='append', default=None)
        if name == 'stage':
            sub.add_argument('--stage', required=True)
            sub.add_argument('--key-file', required=True)
            sub.add_argument('--jobs', type=int, default=4)
        else:
            sub.add_argument('--public-key', required=True)
        sub.add_argument('outputs', nargs='+')
    args = parser.parse_args(argv)
    upstreams = UPSTREAMS if args.upstream is None else args.upstream
    if args.command == 'check':
        check(args.outputs, upstreams, args.destination, args.public_key)
        return
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    prepare(args.outputs, args.stage, args.key_file, upstreams, args.destination, args.jobs)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Cache {sys.argv[1] if len(sys.argv) > 1 else "command"} failed: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
