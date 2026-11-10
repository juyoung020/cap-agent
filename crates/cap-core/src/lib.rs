//! CAP core: the agent loop and everything that judges what the model proposes.
//!
//! The model proposes (tool calls, a "done" claim); this crate decides
//! (permission table, edit engine, test gate).

pub mod agent;
pub mod board;
pub mod cancel;
pub mod codemap;
pub mod context;
pub mod edit;
pub mod exec;
pub mod gate;
pub mod mcp;
pub mod model;
pub mod openai;
pub mod perm;
pub mod sandbox;
pub mod skills;
pub mod testing;
pub mod tools;
pub mod view;
pub mod workspace;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
