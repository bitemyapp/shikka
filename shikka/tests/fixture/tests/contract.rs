#[test]
fn cargo_paths_survive_child_chdir() {
    assert_eq!(std::fs::read_to_string("message.txt").unwrap().trim(), "hello");
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_shikka-fixture"))
        .current_dir(std::env::temp_dir()).output().unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"42:hello\n");
    assert!(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml").exists());
}

// qualify.py sets this for Cargo and for Shikka alike.
#[test]
fn caller_environment_reaches_tests() {
    assert_eq!(std::env::var("FIXTURE_CALLER").as_deref(), Ok("visible"));
}
