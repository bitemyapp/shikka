// Reads beyond its own package, as Cargo lets any build script do: the
// workspace's lockfile, and the source of a local dependency.
fn main() {
    let package = std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let lock = std::fs::read_to_string(package.join("../Cargo.lock")).unwrap();
    assert!(lock.contains("name = \"fixture-macro\""));
    let workspace = package.parent().unwrap();
    assert!(workspace.join("Cargo.toml").is_file());
    let outside = std::fs::read_to_string(workspace.join("../outside/src/lib.rs")).unwrap();
    assert!(outside.contains("pub fn unit"));
}
