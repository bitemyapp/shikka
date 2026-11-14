#[cfg(feature = "answer")]
pub fn answer() -> u32 { 40 }
#[cfg(not(feature = "answer"))]
pub fn answer() -> u32 { 0 }
#[test]
fn feature_is_resolved() { assert_eq!(answer(), 40); }
