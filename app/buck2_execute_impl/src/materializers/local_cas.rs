/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is dual-licensed under either the MIT license found in the
 * LICENSE-MIT file in the root directory of this source tree or the Apache
 * License, Version 2.0 found in the LICENSE-APACHE file in the root directory
 * of this source tree. You may select, at your option, one of the
 * above-listed licenses.
*/

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::sync::LazyLock;
use std::sync::Mutex;

/// A NativeLink filesystem CAS on this host (`BUCK2_LOCAL_CAS_DIR`, its
/// content directory) holds every blob the remote cache serves, so cloning
/// one replaces a download. Its names carry a generation; an index maps
/// digests to files and is rescanned on a miss, at most once per ten scan
/// durations (50 ms to 1 s), so scanning takes at most a tenth of the time.
pub(crate) struct LocalCas {
    root: PathBuf,
    index: Mutex<LocalCasIndex>,
    /// Signalled when a scan finishes.
    scan_done: std::sync::Condvar,
}

type LocalCasFiles = HashMap<(String, u64), (u64, PathBuf)>;

#[derive(Default)]
struct LocalCasIndex {
    files: LocalCasFiles,
    /// When the latest scan started, whether it is still running, and how
    /// long the last finished one took.
    scanned: Option<std::time::Instant>,
    scanning: bool,
    scan_took: std::time::Duration,
}

/// Ends a scan, waking its waiters even if the scan unwinds.
struct ScanDone<'a>(&'a LocalCas);

impl Drop for ScanDone<'_> {
    fn drop(&mut self) {
        if let Ok(mut index) = self.0.index.lock() {
            index.scanning = false;
        }
        self.0.scan_done.notify_all();
    }
}

pub(crate) static LOCAL_CAS: LazyLock<Option<LocalCas>> = LazyLock::new(|| {
    let root = std::env::var_os("BUCK2_LOCAL_CAS_DIR")?;
    Some(LocalCas::new(PathBuf::from(root)))
});

impl LocalCas {
    #[cfg(target_os = "macos")]
    pub(crate) fn same_device(&self, device: u64) -> bool {
        use std::os::unix::fs::MetadataExt;
        fs::metadata(&self.root).is_ok_and(|metadata| metadata.dev() == device)
    }

    /// APFS publishers call this after their uploads finish. Ordinary Linux
    /// lookups keep the existing throttled path below, unchanged.
    #[cfg(target_os = "macos")]
    pub(crate) fn refresh_since(&self, since: std::time::Instant) {
        let Ok(mut index) = self.index.lock() else {
            return;
        };
        while index.scanning {
            let Ok(next) = self.scan_done.wait(index) else {
                return;
            };
            index = next;
        }
        if index.scanned.is_some_and(|at| at >= since) {
            return;
        }
        let started = std::time::Instant::now();
        index.scanned = Some(started);
        index.scanning = true;
        drop(index);
        let _done = ScanDone(self);
        let files = Self::scan(&self.root);
        if let Ok(mut index) = self.index.lock() {
            index.files = files;
            index.scan_took = started.elapsed();
        }
    }

    pub(crate) fn new(root: PathBuf) -> Self {
        Self {
            root,
            index: Mutex::new(LocalCasIndex::default()),
            scan_done: std::sync::Condvar::new(),
        }
    }

    fn scan(root: &Path) -> LocalCasFiles {
        let mut files = LocalCasFiles::new();
        // Digest folders of NativeLink's layouts v2 and v1; string keys are not blobs.
        for folder in ["d2", "d"] {
            let Ok(entries) = fs::read_dir(root.join(folder)) else {
                continue;
            };
            for entry in entries.flatten() {
                let name = entry.file_name();
                let Some(name) = name.to_str() else { continue };
                let mut parts = name.split('-');
                let (Some(hash), Some(size)) = (parts.next(), parts.next()) else {
                    continue;
                };
                let generation = parts.next().map_or(Some(0), |g| g.parse().ok());
                let (Ok(size), Some(generation)) = (size.parse::<u64>(), generation) else {
                    continue;
                };
                if hash.len() != 64 || parts.next().is_some() {
                    continue;
                }
                let slot = files
                    .entry((hash.to_owned(), size))
                    .or_insert((generation, entry.path()));
                if generation > slot.0 {
                    *slot = (generation, entry.path());
                }
            }
        }
        files
    }

    /// The scan runs outside the lock: at 500,000 blobs it takes long
    /// enough that holding the lock would stall every other clone. Hits
    /// use the current index meanwhile; misses wait for the scan, which
    /// may find them, as at a daemon's first lookups.
    pub(crate) fn find(&self, hash: &str, size: u64) -> Option<PathBuf> {
        let key = (hash.to_owned(), size);
        let found = |files: &LocalCasFiles| files.get(&key).map(|(_, path)| path.clone());
        let mut index = self.index.lock().ok()?;
        if let Some(path) = found(&index.files) {
            return Some(path);
        }
        if index.scanning {
            let index = self
                .scan_done
                .wait_while(index, |index| index.scanning)
                .ok()?;
            return found(&index.files);
        }
        let interval = (index.scan_took * 10).clamp(
            std::time::Duration::from_millis(50),
            std::time::Duration::from_secs(1),
        );
        if index.scanned.is_some_and(|at| at.elapsed() < interval) {
            return None;
        }
        let started = std::time::Instant::now();
        index.scanned = Some(started);
        index.scanning = true;
        drop(index);
        let _done = ScanDone(self);
        let files = Self::scan(&self.root);
        let mut index = self.index.lock().ok()?;
        index.files = files;
        index.scan_took = started.elapsed();
        found(&index.files)
    }
}
