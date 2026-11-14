include!(concat!(env!("OUT_DIR"), "/generated.rs"));
fn main() {
    assert_eq!(env!("FIXTURE_BUILD_SCRIPT"), "working");
    println!("{}:{}", fixture_macro::answer!() + helper::answer() + GENERATED,
        include_str!("../message.txt").trim());
    // Cargo starts a program in the caller's directory with the caller's environment.
    if let Ok(probe) = std::env::var("FIXTURE_PROBE") {
        let directory = std::env::current_dir().unwrap();
        println!("{probe}:{}", directory.file_name().unwrap().to_string_lossy());
    }
}
