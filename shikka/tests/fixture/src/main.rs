include!(concat!(env!("OUT_DIR"), "/generated.rs"));
fn main() {
    assert_eq!(env!("FIXTURE_BUILD_SCRIPT"), "working");
    println!("{}:{}", fixture_macro::answer!() + helper::answer() + GENERATED,
        include_str!("../message.txt").trim());
}
