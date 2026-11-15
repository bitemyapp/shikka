extern crate proc_macro;
#[proc_macro]
pub fn answer(_: proc_macro::TokenStream) -> proc_macro::TokenStream { "1u32".parse().unwrap() }

// Deliberately different in each rustc process, like a random constant macro.
// Metadata and objects must come from the same expansion.
#[proc_macro]
pub fn compilation_id(_: proc_macro::TokenStream) -> proc_macro::TokenStream {
    std::process::id().to_string().parse().unwrap()
}
