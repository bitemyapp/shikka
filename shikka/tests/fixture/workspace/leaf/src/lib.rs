// Instantiated in the crate that calls it, from the body in this crate's metadata.
pub fn pass<T>(value: T) -> T { value }
// Compiled here: the caller has only its signature until the program is linked.
#[inline(never)]
pub fn zero() -> u32 { 0 }
