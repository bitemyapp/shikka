import argparse
import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from . import cas_dedupe



def share_with_cas(content_dirs, store_changed, journal, store=None):
    """After a build, share extents between the CAS and this host's other copies.

    Uploaded outputs are listed in the journal; content-store objects that the
    CAS also holds are matched by name. One physical copy remains of each."""
    if sys.platform == "darwin":
        # The native engine shares APFS outputs before publication. Replacing
        # an inode from this unlocked post-build pass could lose a later write.
        return 0
    reports = []
    blobs = functools.cache(lambda: cas_dedupe.scan(content_dirs))  # One scan for both passes.

    def timed(run):
        start = time.monotonic()
        reports.append(dict(run(), seconds=round(time.monotonic() - start, 3)))
    # The store pass moves CAS blobs onto store objects' extents, and the journal
    # pass moves outputs onto CAS blobs' extents; in this order all end on one copy.
    store = store or os.environ.get("BUCK2_LOCAL_CONTENT_STORE")
    if store and store_changed:
        timed(lambda: cas_dedupe.dedupe(content_dirs, [Path(store)], blobs))
    if journal is not None:
        timed(lambda: cas_dedupe.share_journal(content_dirs, journal, blobs))
    if journal is not None and reports:
        # The last pass, for inspection: nothing in it is secret.
        journal.with_name("local-cas-report.json").write_text(json.dumps(reports, indent=2) + "\n")
    if any(report["cross_filesystem"] for report in reports):
        print("Local CAS is on another filesystem than this worktree; extents not shared.", file=sys.stderr)
        return 0
    problems = [problem for report in reports for problem in report.get("differs", []) + report["errors"]]
    if problems:
        print("Local CAS sharing found mismatched or unreadable files: " + json.dumps(problems[:5]), file=sys.stderr)
        return 1
    return 0


def configure_content_store(root, args):
    """Enable shared reflinks where supported; never substitute copied CAS files."""
    names = ["BUCK2_LOCAL_CONTENT_STORE", "BUCK2_LOCAL_CONTENT_STORE_MAX_BYTES", "BUCK2_LOCAL_CONTENT_STORE_MAX_ENTRIES"]
    selection = args.local_content_store
    if selection is None:
        selection = os.environ.get(names[0], "auto") or "off"
    if selection == "off":
        for name in names:
            os.environ.pop(name, None)
        return
    if sys.platform == "darwin":
        if selection != "auto":
            raise SystemExit("The standalone Buck content store requires Linux; use --cache managed for APFS sharing.")
        for name in names:
            os.environ.pop(name, None)
        return
    limits = [
        args.local_content_store_max_bytes if args.local_content_store_max_bytes is not None else int(os.environ.get(names[1], 10 * 1024**3)),
        args.local_content_store_max_entries if args.local_content_store_max_entries is not None else int(os.environ.get(names[2], 100000)),
    ]
    if any(limit <= 0 for limit in limits):
        raise SystemExit("Local content store byte and entry limits must be positive")
    automatic = selection == "auto"
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    if not cache_home.is_absolute():
        cache_home = Path.home() / ".cache"
    directory = (cache_home / "shikka/content-v1" if automatic else Path(selection).expanduser()).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = directory.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o777 != 0o700:
        raise SystemExit("Local content store must be owned by this user with mode 0700: " + str(directory))
    outputs = root / "buck-out" / args.isolation_dir
    outputs.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=directory) as source, tempfile.TemporaryFile(dir=outputs) as destination:
            source.write(b"sekka-reflink-probe\n" * 512)
            source.flush()
            fcntl.ioctl(destination.fileno(), 0x40049409, source.fileno())  # Linux FICLONE
    except OSError as error:
        if not automatic or error.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL}:
            raise SystemExit("Local content store cannot reflink into buck-out: " + str(error)) from error
        for name in names:
            os.environ.pop(name, None)
        print("Buck local content store disabled: this output/cache filesystem pair does not support reflinks; use --local-content-store=off to select this explicitly.", file=sys.stderr)
        return
    os.environ.update(zip(names, [str(directory), *(str(limit) for limit in limits)]))


