//! CAP core: the agent loop and everything that judges what the model proposes.
//!
//! The model proposes (tool calls, a "done" claim); this crate decides
//! (permission table, edit engine, test gate).

pub mod agent;
pub mod context;
pub mod edit;
pub mod exec;
pub mod gate;
pub mod model;
pub mod openai;
pub mod perm;
pub mod testing;
pub mod tools;
pub mod workspace;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
