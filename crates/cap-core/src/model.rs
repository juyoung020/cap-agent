//! Messages, tool calls and the `Model` trait every provider implements.

use std::fmt;
use std::future::Future;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
        }
    }
}

/// A tool call exactly as the model wrote it: `args` is the raw JSON text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub args: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    pub role: Role,
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub tool_call_id: Option<String>,
}

impl Message {
    fn plain(role: Role, content: impl Into<String>) -> Self {
        Self { role, content: content.into(), tool_calls: Vec::new(), tool_call_id: None }
    }
    pub fn system(content: impl Into<String>) -> Self {
        Self::plain(Role::System, content)
    }
    pub fn user(content: impl Into<String>) -> Self {
        Self::plain(Role::User, content)
    }
    pub fn assistant(content: impl Into<String>, tool_calls: Vec<ToolCall>) -> Self {
        Self { tool_calls, ..Self::plain(Role::Assistant, content) }
    }
    pub fn tool(id: impl Into<String>, content: impl Into<String>) -> Self {
        Self { tool_call_id: Some(id.into()), ..Self::plain(Role::Tool, content) }
    }
}

/// A tool the model may call. `parameters` is a JSON Schema object.
#[derive(Clone, Debug)]
pub struct ToolSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub parameters: serde_json::Value,
}

#[derive(Clone, Copy, Debug)]
pub struct Request<'a> {
    pub messages: &'a [Message],
    pub tools: &'a [ToolSpec],
    pub max_tokens: u32,
    pub temperature: f32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Finish {
    Stop,
    /// The reply hit the output limit and is cut off.
    Length,
    ToolCalls,
    Other,
}

#[derive(Clone, Debug)]
pub struct Reply {
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub finish: Finish,
    pub usage: Usage,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    /// The key was refused.
    Auth,
    ModelNotFound,
    /// No server answered at the address.
    Unreachable,
    RateLimited,
    /// The request is larger than the model's context window.
    Context,
    Other,
}

#[derive(Clone, Debug)]
pub struct ModelError {
    pub kind: ErrorKind,
    pub message: String,
}

impl ModelError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into() }
    }
}

impl fmt::Display for ModelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let what = match self.kind {
            ErrorKind::Auth => "the API key was refused",
            ErrorKind::ModelNotFound => "the server does not have this model",
            ErrorKind::Unreachable => "no model server answered",
            ErrorKind::RateLimited => "the server is busy",
            ErrorKind::Context => "the request is larger than the context window",
            ErrorKind::Other => "the model call failed",
        };
        write!(f, "{what}: {}", self.message)
    }
}

impl std::error::Error for ModelError {}

/// One chat completion. Implemented by the HTTP client and by the scripted model used in tests.
pub trait Model: Sync {
    fn complete(&self, req: Request<'_>) -> impl Future<Output = Result<Reply, ModelError>> + Send;
}
