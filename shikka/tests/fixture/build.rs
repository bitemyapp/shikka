fn main() {
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    std::fs::write(out.join("generated.rs"), "const GENERATED: u32 = 1;").unwrap();
    println!("cargo::rustc-env=FIXTURE_BUILD_SCRIPT=working");
}
