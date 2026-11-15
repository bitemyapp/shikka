pub use fixture_macro::compilation_id;
pub const BUILD_ID: u32 = compilation_id!();
#[inline(never)]
pub fn build_id() -> u32 { BUILD_ID }
