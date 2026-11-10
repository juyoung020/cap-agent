//! Test support: a model that replays a script, and a temporary project folder.
//!
//! Public so the binary crate and integration tests can drive the real loop without a server.

use crate::model::{ErrorKind, Finish, Message, Model, ModelError, Reply, Request, ToolCall, ToolChoice, Usage};
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU32, Ordering};

/// What the loop sent in one call.
#[derive(Clone, Debug)]
pub struct Recorded {
    pub messages: Vec<Message>,
    pub tools: Vec<String>,
    pub max_tokens: u32,
    pub tool_choice: ToolChoice,
}

/// Replies in order; after the script ends it repeats `fallback`, or fails when there is none.
#[derive(Default)]
pub struct ScriptedModel {
    script: Mutex<VecDeque<Result<Reply, ModelError>>>,
    fallback: Option<Reply>,
    requests: Mutex<Vec<Recorded>>,
}

impl ScriptedModel {
    pub fn new(replies: impl IntoIterator<Item = Reply>) -> Self {
        Self { script: Mutex::new(replies.into_iter().map(Ok).collect()), ..Self::default() }
    }

    pub fn repeating(reply: Reply) -> Self {
        Self { fallback: Some(reply), ..Self::default() }
    }

    pub fn push_error(&self, kind: ErrorKind, message: &str) {
        self.script.lock().unwrap().push_back(Err(ModelError::new(kind, message)));
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }

    pub fn calls(&self) -> usize {
        self.requests.lock().unwrap().len()
    }
}

impl Model for ScriptedModel {
    async fn complete(&self, req: Request<'_>) -> Result<Reply, ModelError> {
        self.requests.lock().unwrap().push(Recorded {
            messages: req.messages.to_vec(),
            tools: req.tools.iter().map(|t| t.name.to_string()).collect(),
            max_tokens: req.max_tokens,
            tool_choice: req.tool_choice,
        });
        match self.script.lock().unwrap().pop_front() {
            Some(next) => next,
            None => self.fallback.clone().ok_or_else(|| ModelError::new(ErrorKind::Other, "the script has no more replies")),
        }
    }
}

static CALL: AtomicU32 = AtomicU32::new(0);

/// A reply that calls one tool. `args` is JSON text, passed on exactly as written.
pub fn call(name: &str, args: &str) -> Reply {
    calls(&[(name, args)])
}

pub fn calls(list: &[(&str, &str)]) -> Reply {
    let tool_calls = list
        .iter()
        .map(|(name, args)| ToolCall {
            id: format!("t{}", CALL.fetch_add(1, Ordering::Relaxed)),
            name: name.to_string(),
            args: args.to_string(),
        })
        .collect();
    Reply { content: String::new(), tool_calls, finish: Finish::ToolCalls, usage: Usage { prompt_tokens: 0, completion_tokens: 20 } }
}

/// A reply with text only, which the loop reads as a "done" claim.
pub fn say(text: &str) -> Reply {
    Reply {
        content: text.to_string(),
        tool_calls: Vec::new(),
        finish: Finish::Stop,
        usage: Usage { prompt_tokens: 0, completion_tokens: 10 },
    }
}

/// A reply that hit the output limit.
pub fn cut(text: &str) -> Reply {
    Reply { finish: Finish::Length, ..say(text) }
}

/// A folder under the system temp directory, removed when dropped.
pub struct TempDir(PathBuf);

impl TempDir {
    pub fn new(tag: &str) -> Self {
        static N: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!("cap-{tag}-{}-{}", std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("temp dir");
        TempDir(dir)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }

    /// Write a file, creating its folders.
    pub fn write(&self, rel: &str, text: &str) -> &Self {
        let p = self.0.join(rel);
        std::fs::create_dir_all(p.parent().expect("parent")).expect("dirs");
        std::fs::write(p, text).expect("write");
        self
    }

    pub fn read(&self, rel: &str) -> String {
        std::fs::read_to_string(self.0.join(rel)).expect("read")
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
