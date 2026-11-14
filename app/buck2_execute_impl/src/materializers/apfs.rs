/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is dual-licensed under either the MIT license found in the
 * LICENSE-MIT file in the root directory of this source tree or the Apache
 * License, Version 2.0 found in the LICENSE-APACHE file in the root directory
 * of this source tree. You may select, at your option, one of the
 * above-listed licenses.
*/

//! APFS local-CAS snapshots. Only private staging files and action-owned,
//! unpublished outputs may be replaced. NativeLink owns its blob namespace:
//! we read it through open descriptors and never modify it directly.

use std::ffi::CString;
use std::fs::File;
use std::fs::OpenOptions;
use std::fs::Permissions;
use std::fs::{self};
use std::io::Read;
use std::io::Seek;
use std::io::SeekFrom;
use std::io::{self};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::path::PathBuf;
use std::sync::LazyLock;

use buck2_common::cas_digest::DigestAlgorithm;
use buck2_common::cas_digest::DigestAlgorithmFamily;
use buck2_common::file_ops::metadata::FileDigest;
use buck2_common::file_ops::metadata::FileMetadata;
use buck2_directory::directory::directory::Directory;
use buck2_directory::directory::directory_iterator::DirectoryIterator;
use buck2_directory::directory::directory_iterator::DirectoryIteratorPathStack;
use buck2_directory::directory::entry::DirectoryEntry;
use buck2_directory::directory::walk::unordered_entry_walk;
use buck2_execute::digest::CasDigestToReExt;
use buck2_execute::directory::ActionDirectory;
use buck2_execute::directory::ActionDirectoryEntry;
use buck2_execute::directory::ActionDirectoryMember;
use buck2_execute::materialize::materializer::CasDownloadInfo;
use buck2_execute::re::manager::ManagedRemoteExecutionClient;
use buck2_execute::re::presence::NegativeCache;
use buck2_fs::paths::abs_norm_path::AbsNormPathBuf;
use dupe::Dupe;
use remote_execution::NamedDigest;
use tempfile::TempDir;
use tokio::sync::Semaphore;

use super::local_cas::LOCAL_CAS;

// Darwin constants, deliberately separate from the Linux FICLONE backend.
const O_NOFOLLOW: i32 = 0x100;
const O_NONBLOCK: i32 = 0x4;
const CLONE_NOOWNERCOPY: u32 = 0x2;
const PREFIX: &str = ".buck2-apfs-";
const BATCH_FILES: usize = 16;
const BATCH_BYTES: u64 = 64 * 1024 * 1024;
static UPLOADS: LazyLock<Semaphore> = LazyLock::new(|| Semaphore::new(4));

unsafe extern "C" {
    fn fclonefileat(src: i32, dst_dir: i32, name: *const std::ffi::c_char, flags: u32) -> i32;
    fn geteuid() -> u32;
}

fn error(message: impl std::fmt::Display) -> buck2_error::Error {
    buck2_error::buck2_error!(buck2_error::ErrorTag::MaterializationError, "{}", message)
}

fn unsupported(error: &io::Error) -> bool {
    // EXDEV, ENOTSUP, ENOSYS. Other errors must not masquerade as absent COW.
    matches!(error.raw_os_error(), Some(18 | 45 | 78))
}

fn open_regular(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW | O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other(
            "APFS snapshot source is not a regular file",
        ));
    }
    Ok(file)
}

fn same_inode(a: &fs::Metadata, b: &fs::Metadata) -> bool {
    a.dev() == b.dev() && a.ino() == b.ino()
}

fn publish_owned(
    snapshot: Snapshot,
    path: &Path,
    before: &fs::Metadata,
    executable: bool,
) -> buck2_error::Result<()> {
    let current = fs::symlink_metadata(path)?;
    if !same_inode(before, &current)
        || before.len() != current.len()
        || before.mode() != current.mode()
        || before.mtime() != current.mtime()
        || before.mtime_nsec() != current.mtime_nsec()
        || before.ctime() != current.ctime()
        || before.ctime_nsec() != current.ctime_nsec()
    {
        return Err(error("Action-owned output changed during APFS CAS upload"));
    }
    snapshot.publish(path, executable)
}

/// Clean only recognized, privately owned, unlocked capture directories. A
/// process that crashes leaves no published output; the next capture reclaims
/// its snapshots. A live upload holds its lock across transport retries.
fn clean_stale(parent: &Path) -> io::Result<()> {
    let uid = unsafe { geteuid() };
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        if !entry
            .file_name()
            .as_encoded_bytes()
            .starts_with(PREFIX.as_bytes())
        {
            continue;
        }
        let path = entry.path();
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        if !metadata.is_dir() || metadata.uid() != uid || metadata.mode() & 0o7777 != 0o700 {
            continue;
        }
        let lock_path = path.join("lock");
        let Ok(lock) = open_regular(&lock_path) else {
            // Only remove a crash-before-lock directory if it is still empty.
            let _ignored = fs::remove_dir(&path);
            continue;
        };
        let locked = lock.metadata()?;
        if locked.uid() != uid
            || locked.nlink() != 1
            || locked.mode() & 0o7777 != 0o600
            || lock.try_lock().is_err()
        {
            continue;
        }
        if !fs::symlink_metadata(&path).is_ok_and(|now| same_inode(&metadata, &now))
            || !fs::symlink_metadata(&lock_path).is_ok_and(|now| same_inode(&locked, &now))
        {
            continue;
        }
        // Do not recursively remove unrecognized entries, or follow links.
        let Ok(entries) = fs::read_dir(&path) else {
            continue;
        };
        let names: Vec<_> = entries
            .map(|entry| entry.map(|e| e.file_name()))
            .collect::<io::Result<_>>()?;
        if names.iter().any(|name| name != "lock" && name != "blob") {
            continue;
        }
        match fs::remove_file(path.join("blob")) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(_) => continue,
        }
        fs::remove_file(lock_path)?;
        fs::remove_dir(path)?;
    }
    Ok(())
}

pub(crate) struct Snapshot {
    directory: TempDir,
    _lock: File,
    file: File,
}

impl Snapshot {
    fn clone(source: &File, parent: &Path) -> io::Result<Self> {
        // Serialize creation/scavenging in this parent. Otherwise a scavenger
        // could remove a new directory in the create-before-lock window.
        // The directory descriptor is released before network or hashing work.
        let parent_guard = File::open(parent)?;
        parent_guard.lock()?;
        clean_stale(parent)?;
        let directory = tempfile::Builder::new().prefix(PREFIX).tempdir_in(parent)?;
        fs::set_permissions(directory.path(), Permissions::from_mode(0o700))?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(O_NOFOLLOW)
            .open(directory.path().join("lock"))?;
        lock.try_lock().map_err(io::Error::from)?;
        if !same_inode(
            &lock.metadata()?,
            &fs::symlink_metadata(directory.path().join("lock"))?,
        ) {
            return Err(io::Error::other("APFS capture lock was replaced"));
        }
        let dir = File::open(directory.path())?;
        let name = CString::new("blob").unwrap();
        // SAFETY: both descriptors are live, `name` is NUL-terminated and
        // belongs to our private directory; fclonefileat requires a fresh name.
        if unsafe {
            fclonefileat(
                source.as_raw_fd(),
                dir.as_raw_fd(),
                name.as_ptr(),
                CLONE_NOOWNERCOPY,
            )
        } != 0
        {
            return Err(io::Error::last_os_error());
        }
        let file = open_regular(&directory.path().join("blob"))?;
        if same_inode(&source.metadata()?, &file.metadata()?) || file.metadata()?.nlink() != 1 {
            return Err(io::Error::other(
                "APFS clone must have an independent inode",
            ));
        }
        Ok(Self {
            directory,
            _lock: lock,
            file,
        })
    }

    fn path(&self) -> PathBuf {
        self.directory.path().join("blob")
    }

    fn verify(&mut self, expected: &FileDigest) -> buck2_error::Result<()> {
        if self.file.metadata()?.len() != expected.size() {
            return Err(error("APFS snapshot size differs from its digest"));
        }
        self.file.seek(SeekFrom::Start(0))?;
        if expected.raw_digest().algorithm() != DigestAlgorithmFamily::Sha256 {
            return Err(error("APFS local CAS requires SHA-256"));
        }
        let algorithm = DigestAlgorithm::Sha256;
        let actual = FileDigest::from_reader_for_algorithm(
            (&mut self.file).take(expected.size().saturating_add(1)),
            algorithm,
        )?;
        if actual != *expected {
            return Err(error(format!(
                "APFS snapshot digest mismatch: expected {expected}, got {actual}"
            )));
        }
        Ok(())
    }

    fn publish(self, destination: &Path, executable: bool) -> buck2_error::Result<()> {
        self.file
            .set_permissions(Permissions::from_mode(if executable {
                0o755
            } else {
                0o644
            }))?;
        self.file.sync_all()?;
        // Caller owns this destination (private download or unpublished action
        // output). No post-build caller is permitted to replace a live view.
        fs::rename(self.path(), destination)?;
        File::open(
            destination
                .parent()
                .ok_or_else(|| error("Missing APFS destination parent"))?,
        )?
        .sync_all()?;
        Ok(())
    }
}

fn cas_key(digest: &FileDigest) -> Option<(String, u64)> {
    if digest.raw_digest().algorithm() != DigestAlgorithmFamily::Sha256 {
        return None;
    }
    let rendered = digest.to_string();
    Some((rendered.split_once(':')?.0.to_owned(), digest.size()))
}

/// Fill a private, empty Download stage. Download::finish performs the digest
/// check and mode normalization before final publication, just as for gRPC.
pub(crate) fn clone_cached(digest: &FileDigest, destination: &Path) -> bool {
    let Some((hash, size)) = cas_key(digest) else {
        return false;
    };
    let Some(path) = LOCAL_CAS.as_ref().and_then(|cas| cas.find(&hash, size)) else {
        return false;
    };
    let attempt = || -> io::Result<()> {
        let source = open_regular(&path)?;
        if source.metadata()?.len() != size {
            return Err(io::Error::other("Local CAS blob size mismatch"));
        }
        let stage = Snapshot::clone(
            &source,
            destination
                .parent()
                .ok_or_else(|| io::Error::other("Missing stage parent"))?,
        )?;
        fs::rename(stage.path(), destination)?;
        Ok(())
    };
    attempt().is_ok()
}

pub(crate) fn collect<D: ActionDirectory>(
    path: &AbsNormPathBuf,
    entry: &ActionDirectoryEntry<D>,
    files: &mut Vec<(AbsNormPathBuf, FileMetadata)>,
) {
    let mut walk = unordered_entry_walk(entry.as_ref().map_dir(Directory::as_ref));
    while let Some((relative, entry)) = walk.next() {
        if let DirectoryEntry::Leaf(ActionDirectoryMember::File(metadata)) = entry {
            files.push((path.join(relative.get()), metadata.dupe()));
        }
    }
}

/// Called after successful execution and hashing, before declare/publish,
/// while the executor still holds its output lease and host-sharing permit.
/// Network awaits only retain private snapshots. Publication is synchronous;
/// no detached blocking task can outlive those ownership guards on cancel.
pub(crate) async fn share_owned_outputs(
    files: &[(AbsNormPathBuf, FileMetadata)],
    client: &ManagedRemoteExecutionClient,
) -> buck2_error::Result<()> {
    let Some(cas) = LOCAL_CAS.as_ref() else {
        return Ok(());
    };
    let _permit = UPLOADS.acquire().await.map_err(error)?;
    let mut originals = Vec::new();
    let mut offset = 0;
    while offset < files.len() {
        let mut snapshots = Vec::new();
        let mut bytes: u64 = 0;
        while offset < files.len() && snapshots.len() < BATCH_FILES {
            let (path, metadata) = &files[offset];
            let size = metadata.digest.data().size();
            if !snapshots.is_empty() && bytes.saturating_add(size) > BATCH_BYTES {
                break;
            }
            offset += 1;
            if size == 0 || cas_key(metadata.digest.data()).is_none() {
                continue;
            }
            let source = open_regular(path.as_path())?;
            let before = source.metadata()?;
            if !cas.same_device(before.dev()) {
                continue; // A separate CAS volume uses the normal upload path.
            }
            let mut snapshot = match Snapshot::clone(&source, path.as_path().parent().unwrap()) {
                Ok(snapshot) => snapshot,
                Err(e) if unsupported(&e) => continue,
                Err(e) => return Err(e.into()),
            };
            snapshot.verify(metadata.digest.data())?;
            bytes = bytes.saturating_add(size);
            originals.push((path, metadata, before));
            snapshots.push((snapshot, metadata));
        }
        if snapshots.is_empty() {
            continue;
        }
        let info = CasDownloadInfo::new_uploaded(client.use_case);
        let present = match client
            .check_presence(
                snapshots.iter().map(|(_, m)| m.digest.dupe()).collect(),
                NegativeCache::Bypassed,
                &info,
            )
            .await
        {
            Ok(present) => present,
            Err(error) => {
                tracing::debug!(%error, "APFS early upload unavailable; ordinary cache upload remains authoritative");
                return Ok(());
            }
        };
        let mut missing = Vec::new();
        for ((snapshot, metadata), present) in snapshots.iter().zip(present) {
            if present.is_none() {
                missing.push(NamedDigest {
                    name: snapshot.path().to_string_lossy().into_owned(),
                    digest: metadata.digest.to_re(),
                    ..Default::default()
                });
            }
        }
        if !missing.is_empty() {
            if let Err(error) = client
                .upload_files_and_directories(missing, Vec::new(), Vec::new())
                .await
            {
                tracing::debug!(%error, "APFS early upload failed; ordinary cache upload remains authoritative");
                return Ok(());
            }
        }
        // Transport and every retry completed before snapshots are dropped.
    }
    if originals.is_empty() {
        return Ok(());
    }
    // A successful upload makes the generation visible. Bypass the ordinary
    // miss-throttle once per action, not once per file; never write CAS paths.
    cas.refresh_since(std::time::Instant::now());
    for (path, metadata, before) in originals {
        let Some((hash, size)) = cas_key(metadata.digest.data()) else {
            continue;
        };
        let Some(blob) = cas.find(&hash, size) else {
            continue;
        };
        let source = match open_regular(&blob) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue, // Concurrent eviction.
            Err(e) => return Err(e.into()),
        };
        let mut snapshot = match Snapshot::clone(&source, path.as_path().parent().unwrap()) {
            Ok(snapshot) => snapshot,
            Err(e) if unsupported(&e) => continue,
            Err(e) => return Err(e.into()),
        };
        snapshot.verify(metadata.digest.data())?;
        publish_owned(snapshot, path.as_path(), &before, metadata.is_executable)?;
    }
    Ok(())
}

/// Materializer copies also own their destination. Use a verified APFS clone
/// when local-CAS mode is selected, including placeholder -> content path.
pub(crate) fn copy_owned(
    source: &Path,
    destination: &Path,
    metadata: &FileMetadata,
) -> buck2_error::Result<bool> {
    if cas_key(metadata.digest.data()).is_none() {
        return Ok(false);
    }
    let source = open_regular(source)?;
    let mut stage = match Snapshot::clone(
        &source,
        destination
            .parent()
            .ok_or_else(|| error("Missing copy parent"))?,
    ) {
        Ok(stage) => stage,
        Err(e) if unsupported(&e) => return Ok(false),
        Err(e) => return Err(e.into()),
    };
    stage.verify(metadata.digest.data())?;
    stage.publish(destination, metadata.is_executable)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use std::io::Write;
    use std::os::unix::fs::symlink;

    use buck2_execute::digest_config::DigestConfig;

    use super::*;

    fn digest(bytes: &[u8]) -> FileDigest {
        let config = DigestConfig::leak_new(vec![DigestAlgorithm::Sha256], None).unwrap();
        FileDigest::from_content(bytes, config.cas_digest_config())
    }

    #[test]
    fn verified_clone_is_independent_writable_and_survives_eviction() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("cas");
        let data = vec![93; 128 * 1024];
        fs::write(&source, &data)?;
        fs::set_permissions(&source, Permissions::from_mode(0o444))?;
        let original = fs::metadata(&source)?;
        let fd = open_regular(&source)?;
        let mut stage = Snapshot::clone(&fd, root.path())?;
        stage.verify(&digest(&data))?;
        assert!(!same_inode(&original, &stage.file.metadata()?));
        assert_eq!(stage.file.metadata()?.nlink(), 1);
        let output = root.path().join("output");
        stage.publish(&output, true)?;
        assert_eq!(fs::metadata(&output)?.mode() & 0o777, 0o755);
        assert_eq!(fs::metadata(&source)?.mode() & 0o777, 0o444);
        fs::write(&output, b"a later compiler writes this view")?;
        assert_eq!(fs::read(&source)?, data);
        // An open donor pins its extents even after NativeLink evicts its name.
        fs::remove_file(&source)?;
        let mut second = Snapshot::clone(&fd, root.path())?;
        second.verify(&digest(&data))?;
        second.publish(&output, false)?;
        assert_eq!(fs::read(&output)?, data);
        assert_eq!(fs::metadata(output)?.mode() & 0o777, 0o644);
        Ok(())
    }

    #[test]
    fn mismatch_never_replaces_destination_and_snapshot_survives_source_rewrite()
    -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("source");
        let dest = root.path().join("dest");
        fs::write(&source, b"correct")?;
        fs::write(&dest, b"previous")?;
        let mut stage = Snapshot::clone(&open_regular(&source)?, root.path())?;
        fs::write(&source, b"changed")?;
        stage.verify(&digest(b"correct"))?;
        drop(stage);
        let mut bad = Snapshot::clone(&open_regular(&source)?, root.path())?;
        assert!(bad.verify(&digest(b"correct")).is_err());
        assert!(bad.verify(&digest(b"wrong size")).is_err());
        assert_eq!(fs::read(dest)?, b"previous");
        Ok(())
    }

    #[test]
    fn later_output_writes_and_replacements_are_not_overwritten() -> buck2_error::Result<()> {
        for replace in [false, true] {
            let root = tempfile::tempdir()?;
            let output = root.path().join("output");
            fs::write(&output, b"initial")?;
            let before = fs::metadata(&output)?;
            let stage = Snapshot::clone(&open_regular(&output)?, root.path())?;
            if replace {
                fs::remove_file(&output)?;
            }
            fs::write(&output, b"changed")?;
            assert!(publish_owned(stage, &output, &before, false).is_err());
            assert_eq!(fs::read(output)?, b"changed");
        }
        Ok(())
    }

    #[test]
    fn rejects_symlinks_and_nonregular_donors_without_blocking() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("source");
        fs::write(&source, b"data")?;
        symlink(&source, root.path().join("link"))?;
        assert!(open_regular(&root.path().join("link")).is_err());
        assert!(open_regular(root.path()).is_err());
        unsafe extern "C" {
            fn mkfifo(path: *const std::ffi::c_char, mode: u16) -> i32;
        }
        let fifo = root.path().join("fifo");
        let name = CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(open_regular(&fifo).is_err());
        Ok(())
    }

    #[test]
    fn stale_capture_recovery_preserves_live_and_unrecognized_paths() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("source");
        fs::write(&source, b"data")?;
        let fd = open_regular(&source)?;
        let live = Snapshot::clone(&fd, root.path())?;
        let stale = Snapshot::clone(&fd, root.path())?;
        let Snapshot {
            directory,
            _lock,
            file,
        } = stale;
        let stale_path = directory.keep();
        drop((_lock, file)); // Simulate process death after capture.
        let unrecognized = root.path().join(format!("{PREFIX}unknown"));
        fs::create_dir(&unrecognized)?;
        fs::write(unrecognized.join("keep"), b"not ours")?;
        let _next = Snapshot::clone(&fd, root.path())?;
        assert!(!stale_path.exists());
        assert_eq!(fs::read(live.path())?, b"data");
        assert_eq!(fs::read(unrecognized.join("keep"))?, b"not ours");
        Ok(())
    }

    #[test]
    fn sparse_clone_preserves_holes_and_donor_mode() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("sparse");
        let mut writer = File::create(&source)?;
        writer.write_all(b"start")?;
        writer.seek(SeekFrom::Start(16 * 1024 * 1024))?;
        writer.write_all(b"end")?;
        // APFS may allocate a seek-created gap. Explicitly punch it so this
        // test actually supplies sparse input rather than assuming Linux rules.
        #[repr(C)]
        struct Punch {
            flags: u32,
            reserved: u32,
            offset: i64,
            length: i64,
        }
        unsafe extern "C" {
            fn fcntl(fd: i32, command: i32, ...) -> i32;
        }
        let hole = Punch {
            flags: 0,
            reserved: 0,
            offset: 4096,
            length: 16 * 1024 * 1024 - 4096,
        };
        assert_eq!(unsafe { fcntl(writer.as_raw_fd(), 99, &hole) }, 0);
        writer.sync_all()?;
        assert!(writer.metadata()?.blocks() * 512 < writer.metadata()?.len() / 4);
        drop(writer);
        let stage = Snapshot::clone(&open_regular(&source)?, root.path())?;
        assert!(stage.file.metadata()?.blocks() * 512 < stage.file.metadata()?.len() / 4);
        assert_eq!(fs::read(stage.path())?, fs::read(source)?);
        Ok(())
    }

    #[test]
    fn cas_refresh_sees_new_generations_without_the_miss_throttle() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let d2 = root.path().join("d2");
        fs::create_dir(&d2)?;
        let cas = super::super::local_cas::LocalCas::new(root.path().to_owned());
        let hash = "a".repeat(64);
        assert!(cas.find(&hash, 1).is_none());
        let path = d2.join(format!("{hash}-1-7"));
        fs::write(&path, b"x")?;
        cas.refresh_since(std::time::Instant::now());
        assert_eq!(cas.find(&hash, 1), Some(path.clone()));
        fs::remove_file(path)?;
        let path = d2.join(format!("{hash}-1-8"));
        fs::write(&path, b"x")?;
        cas.refresh_since(std::time::Instant::now());
        assert_eq!(cas.find(&hash, 1), Some(path));
        Ok(())
    }

    #[test]
    fn concurrent_captures_do_not_scavenge_live_stages() -> buck2_error::Result<()> {
        let root = tempfile::tempdir()?;
        let source = root.path().join("source");
        fs::write(&source, vec![9; 32768])?;
        std::thread::scope(|scope| {
            let workers: Vec<_> = (0..8)
                .map(|i| {
                    let source = &source;
                    let parent = root.path();
                    scope.spawn(move || -> buck2_error::Result<()> {
                        for _ in 0..20 {
                            let mut stage = Snapshot::clone(&open_regular(source)?, parent)?;
                            stage.verify(&digest(&vec![9; 32768]))?;
                            stage.publish(&parent.join(format!("view-{i}")), false)?;
                        }
                        Ok(())
                    })
                })
                .collect();
            for worker in workers {
                worker.join().unwrap().unwrap();
            }
        });
        assert!(!fs::read_dir(root.path())?.any(|entry| {
            entry
                .unwrap()
                .file_name()
                .as_encoded_bytes()
                .starts_with(PREFIX.as_bytes())
        }));
        Ok(())
    }

    #[test]
    fn different_volume_returns_unsupported_without_copying() -> buck2_error::Result<()> {
        let Some(other) = std::env::var_os("SEKKA_APFS_TEST_OTHER_VOLUME") else {
            eprintln!("Cross-volume APFS check skipped: no second disposable volume supplied");
            return Ok(());
        };
        let root = tempfile::tempdir()?;
        let source = root.path().join("source");
        fs::write(&source, b"donor")?;
        let destination = tempfile::tempdir_in(other)?;
        assert_ne!(
            fs::metadata(&source)?.dev(),
            fs::metadata(destination.path())?.dev()
        );
        let error = Snapshot::clone(&open_regular(&source)?, destination.path())
            .err()
            .expect("different volumes cannot share extents");
        assert!(unsupported(&error), "{error}");
        assert_eq!(fs::read_dir(destination.path())?.count(), 0);
        assert_eq!(fs::read(source)?, b"donor");
        Ok(())
    }
}
