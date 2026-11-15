// No direct dependency on the proc-macro crate: the macro is re-exported.
pub const BUILD_ID: u32 = macro_source::compilation_id!();
#[inline(never)]
pub fn build_id() -> u32 { BUILD_ID }
pub fn check() {
    assert_eq!(macro_source::BUILD_ID, macro_source::build_id());
    assert_eq!(BUILD_ID, build_id());
}
