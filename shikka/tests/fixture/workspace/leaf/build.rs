// A macro used only to compile a build script cannot be re-exported by the
// library. The library must remain eligible for metadata pipelining.
fn main() {
    assert_ne!(fixture_macro::compilation_id!(), 0);
}
