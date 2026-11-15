fn main() {
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::write(out.join("generated.rs"), "const GENERATED: u32 = 1;").unwrap();
    println!("cargo::rustc-env=FIXTURE_BUILD_SCRIPT=working");
    // The manifest directory compiled into a build script must still name its
    // package where the script runs.
    let compiled = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("message.txt");
    assert_eq!(std::fs::read_to_string(compiled).unwrap().trim(), "hello");
}
