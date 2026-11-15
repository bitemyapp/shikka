#[cfg(feature = "answer")]
pub fn answer() -> u32 { 39 + leaf::pass(outside::unit()) + leaf::zero() }
#[cfg(not(feature = "answer"))]
pub fn answer() -> u32 { 0 }
#[test]
fn feature_is_resolved() { assert_eq!(answer(), 40); }
