//! Client for OpenAI-compatible chat completion servers (vLLM, Ollama, LM Studio, llama.cpp, hosted APIs).
//!
//! `build_body` and `parse_reply` are pure so the wire format is tested without a network.

use crate::model::{ErrorKind, Finish, Message, Model, ModelError, Reply, Request, Role, ToolCall, ToolChoice, Usage};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

/// Called with each piece of reply text while it arrives.
pub type OnText<'a> = &'a (dyn Fn(&str) + Sync);

pub struct OpenAiClient {
    http: reqwest::Client,
    base_url: String,
    api_key: String,
    model: String,
    /// Send `chat_template_kwargs.enable_thinking = false` (vLLM with a Qwen-style chat template).
    pub disable_thinking: bool,
    /// First wait before a retry; doubles each time, capped at 60 s.
    pub backoff: Duration,
    /// Retries when the server answers 429. A shared server can stay busy for a while.
    pub rate_limit_retries: u32,
    /// Longest silence allowed between two pieces of a streamed reply.
    pub idle: Duration,
    /// Set once this server has refused `tool_choice`; later calls leave it out.
    no_tool_choice: AtomicBool,
    /// Set once this server has refused streaming; later calls ask for whole replies.
    no_stream: AtomicBool,
}

impl OpenAiClient {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>, model: impl Into<String>) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(600))
            .build()
            .expect("HTTP client");
        Self {
            http,
            base_url: base_url.into().trim_end_matches('/').to_string(),
            api_key: api_key.into(),
            model: model.into(),
            disable_thinking: false,
            backoff: Duration::from_secs(3),
            rate_limit_retries: 6,
            idle: Duration::from_secs(120),
            no_tool_choice: AtomicBool::new(false),
            no_stream: AtomicBool::new(false),
        }
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// Keep the key out of anything shown or logged, even when a server echoes it back.
    fn scrub(&self, text: &str) -> String {
        if self.api_key.len() >= 6 { text.replace(&self.api_key, "***") } else { text.to_string() }
    }
}

fn wire_message(m: &Message) -> Value {
    match m.role {
        Role::Assistant if !m.tool_calls.is_empty() => {
            let calls: Vec<Value> = m
                .tool_calls
                .iter()
                .map(|c| json!({"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.args}}))
                .collect();
            json!({"role": "assistant", "content": m.content, "tool_calls": calls})
        }
        Role::Tool => json!({
            "role": "tool",
            "tool_call_id": m.tool_call_id.as_deref().unwrap_or(""),
            "content": m.content,
        }),
        role => json!({"role": role.as_str(), "content": m.content}),
    }
}

pub fn build_body(model: &str, req: &Request<'_>, disable_thinking: bool) -> Value {
    let messages: Vec<Value> = req.messages.iter().map(wire_message).collect();
    let mut body = json!({
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    });
    if !req.tools.is_empty() {
        let tools: Vec<Value> = req
            .tools
            .iter()
            .map(|t| {
                json!({"type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": t.parameters,
                }})
            })
            .collect();
        body["tools"] = Value::Array(tools);
        if req.tool_choice == ToolChoice::Required {
            body["tool_choice"] = json!("required");
        }
    }
    if disable_thinking {
        body["chat_template_kwargs"] = json!({"enable_thinking": false});
    }
    body
}

/// Remove `<think>…</think>` blocks a reasoning model may leave in the text.
fn strip_think(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("<think>") {
        out.push_str(&rest[..start]);
        match rest[start..].find("</think>") {
            Some(end) => rest = &rest[start + end + "</think>".len()..],
            None => {
                rest = "";
                break;
            }
        }
    }
    out.push_str(rest);
    out.trim().to_string()
}

/// Tool calls written as text (`<tool_call>{"name":…,"arguments":…}</tool_call>`), which is what a
/// server without a tool-call parser passes through. Returns the remaining text and the calls.
pub fn text_tool_calls(content: &str) -> (String, Vec<ToolCall>) {
    const OPEN: &str = "<tool_call>";
    const CLOSE: &str = "</tool_call>";
    let mut calls = Vec::new();
    let mut text = String::new();
    let mut rest = content;
    while let Some(start) = rest.find(OPEN) {
        text.push_str(&rest[..start]);
        let after = &rest[start + OPEN.len()..];
        let (inner, next) = match after.find(CLOSE) {
            Some(end) => (&after[..end], &after[end + CLOSE.len()..]),
            None => (after, ""),
        };
        match serde_json::from_str::<Value>(inner.trim()) {
            Ok(v) if v["name"].is_string() => {
                let args = match v.get("arguments").or_else(|| v.get("parameters")) {
                    Some(Value::String(s)) => s.clone(),
                    Some(other) => other.to_string(),
                    None => "{}".to_string(),
                };
                calls.push(ToolCall {
                    id: format!("call_{}", calls.len()),
                    name: v["name"].as_str().unwrap_or_default().to_string(),
                    args,
                });
            }
            _ => {
                text.push_str(OPEN);
                text.push_str(inner);
            }
        }
        rest = next;
    }
    text.push_str(rest);
    (text.trim().to_string(), calls)
}

pub fn parse_reply(v: &Value) -> Result<Reply, ModelError> {
    let choice = v["choices"].get(0).ok_or_else(|| ModelError::new(ErrorKind::Other, "the server reply has no choices"))?;
    let msg = &choice["message"];
    let mut content = strip_think(msg["content"].as_str().unwrap_or(""));
    let mut tool_calls = Vec::new();
    if let Some(list) = msg["tool_calls"].as_array() {
        for (i, c) in list.iter().enumerate() {
            let f = &c["function"];
            let Some(name) = f["name"].as_str().filter(|n| !n.is_empty()) else { continue };
            let args = match &f["arguments"] {
                Value::String(s) => s.clone(),
                Value::Null => "{}".to_string(),
                other => other.to_string(),
            };
            let id = c["id"].as_str().filter(|s| !s.is_empty()).map_or_else(|| format!("call_{i}"), str::to_string);
            tool_calls.push(ToolCall { id, name: name.to_string(), args });
        }
    }
    if tool_calls.is_empty() && content.contains("<tool_call>") {
        (content, tool_calls) = text_tool_calls(&content);
    }
    let finish = match choice["finish_reason"].as_str() {
        Some("stop") => Finish::Stop,
        Some("length") => Finish::Length,
        Some("tool_calls") => Finish::ToolCalls,
        _ => Finish::Other,
    };
    let tokens = |key: &str| v["usage"][key].as_u64().unwrap_or(0) as u32;
    Ok(Reply {
        content,
        tool_calls,
        finish,
        usage: Usage { prompt_tokens: tokens("prompt_tokens"), completion_tokens: tokens("completion_tokens") },
    })
}

pub fn classify(status: u16, body: &str) -> ErrorKind {
    let low = body.to_lowercase();
    let context = ["context length", "maximum context", "context window", "too many tokens", "max_model_len"];
    match status {
        401 | 403 => ErrorKind::Auth,
        404 => ErrorKind::ModelNotFound,
        429 => ErrorKind::RateLimited,
        400 | 413 | 422 if context.iter().any(|k| low.contains(k)) => ErrorKind::Context,
        _ => ErrorKind::Other,
    }
}

fn short_error(status: u16, body: &str) -> String {
    let detail = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|v| v["error"]["message"].as_str().or_else(|| v["message"].as_str()).or_else(|| v["error"].as_str()).map(str::to_string))
        .unwrap_or_else(|| body.trim().to_string());
    let detail: String = detail.chars().take(400).collect();
    format!("HTTP {status}: {detail}")
}

/// Length of the longest proper prefix of `tag` that `text` ends with.
fn partial_tag(text: &str, tag: &str) -> usize {
    (1..tag.len()).rev().find(|&k| text.ends_with(&tag[..k])).unwrap_or(0)
}

/// Turns the raw pieces of a streamed reply into the text a person should see while it arrives:
/// thinking blocks are left out and the text ends where a written-out tool call begins.
#[derive(Default)]
pub struct Visible {
    held: String,
    thinking: bool,
    stopped: bool,
    started: bool,
}

impl Visible {
    const THINK: &'static str = "<think>";
    const END_THINK: &'static str = "</think>";
    const CALL: &'static str = "<tool_call>";

    pub fn push(&mut self, piece: &str) -> String {
        if self.stopped {
            return String::new();
        }
        self.held.push_str(piece);
        let mut out = String::new();
        loop {
            if self.thinking {
                match self.held.find(Self::END_THINK) {
                    Some(end) => {
                        self.held.drain(..end + Self::END_THINK.len());
                        self.thinking = false;
                    }
                    None => {
                        let cut = self.held.len() - partial_tag(&self.held, Self::END_THINK);
                        self.held.drain(..cut);
                        break;
                    }
                }
                continue;
            }
            let think = self.held.find(Self::THINK);
            let call = self.held.find(Self::CALL);
            match (think, call) {
                (Some(t), c) if c.is_none_or(|c| t < c) => {
                    out.push_str(&self.held[..t]);
                    self.held.drain(..t + Self::THINK.len());
                    self.thinking = true;
                }
                (_, Some(c)) => {
                    out.push_str(&self.held[..c]);
                    self.held.clear();
                    self.stopped = true;
                    break;
                }
                _ => {
                    // A tail that may be the start of a tag waits for the next piece.
                    let keep = partial_tag(&self.held, Self::THINK).max(partial_tag(&self.held, Self::CALL));
                    let cut = self.held.len() - keep;
                    out.push_str(&self.held[..cut]);
                    self.held.drain(..cut);
                    break;
                }
            }
        }
        self.shown(out)
    }

    /// What was still held back when the reply ended.
    pub fn finish(&mut self) -> String {
        if self.thinking || self.stopped {
            return String::new();
        }
        let rest = std::mem::take(&mut self.held);
        self.shown(rest)
    }

    fn shown(&mut self, out: String) -> String {
        if self.started {
            return out;
        }
        let trimmed = out.trim_start();
        if trimmed.is_empty() {
            return String::new();
        }
        self.started = true;
        trimmed.to_string()
    }
}

#[derive(Default)]
struct PartialCall {
    id: String,
    name: String,
    args: String,
}

/// Collects the chunks of a streamed reply into the shape `parse_reply` reads.
#[derive(Default)]
pub struct StreamReply {
    content: String,
    calls: Vec<PartialCall>,
    by_index: HashMap<u64, usize>,
    finish: Option<String>,
    usage: Value,
}

impl StreamReply {
    /// Take one `data:` object. Returns the reply text it added.
    pub fn push(&mut self, v: &Value) -> Result<String, ModelError> {
        if let Some(e) = v.get("error").filter(|e| !e.is_null()) {
            let text = e["message"].as_str().map_or_else(|| e.to_string(), str::to_string);
            let text: String = text.chars().take(400).collect();
            return Err(ModelError::new(classify(400, &text), format!("the server stopped the reply: {text}")));
        }
        if v["usage"].is_object() {
            self.usage = v["usage"].clone();
        }
        let Some(choice) = v["choices"].get(0) else { return Ok(String::new()) };
        if let Some(reason) = choice["finish_reason"].as_str() {
            self.finish = Some(reason.to_string());
        }
        let delta = &choice["delta"];
        for c in delta["tool_calls"].as_array().map(Vec::as_slice).unwrap_or_default() {
            let index = c["index"].as_u64().unwrap_or(0);
            let id = c["id"].as_str().unwrap_or("");
            // A new id on a used index is a new call: some servers number every call 0.
            let slot = match self.by_index.get(&index) {
                Some(&at) if id.is_empty() || self.calls[at].id.is_empty() || self.calls[at].id == id => at,
                _ => {
                    self.calls.push(PartialCall::default());
                    self.by_index.insert(index, self.calls.len() - 1);
                    self.calls.len() - 1
                }
            };
            let call = &mut self.calls[slot];
            if call.id.is_empty() {
                call.id = id.to_string();
            }
            let f = &c["function"];
            if call.name.is_empty() {
                call.name = f["name"].as_str().unwrap_or("").to_string();
            }
            match &f["arguments"] {
                Value::String(s) => call.args.push_str(s),
                Value::Null => {}
                other => call.args = other.to_string(),
            }
        }
        let piece = delta["content"].as_str().unwrap_or("");
        self.content.push_str(piece);
        Ok(piece.to_string())
    }

    pub fn is_empty(&self) -> bool {
        self.content.is_empty() && self.calls.is_empty()
    }

    /// The whole reply. A stream that ended without a finish reason counts as cut off.
    pub fn finish(self) -> Result<Reply, ModelError> {
        if self.is_empty() && self.finish.is_none() {
            return Err(ModelError::new(ErrorKind::Other, "the server ended the reply before sending anything"));
        }
        let calls: Vec<Value> = self
            .calls
            .iter()
            .map(|c| {
                let args = if c.args.is_empty() { "{}" } else { c.args.as_str() };
                json!({"id": c.id, "type": "function", "function": {"name": c.name, "arguments": args}})
            })
            .collect();
        let finish = self.finish.unwrap_or_else(|| "length".to_string());
        parse_reply(&json!({
            "choices": [{"finish_reason": finish, "message": {"content": self.content, "tool_calls": calls}}],
            "usage": self.usage,
        }))
    }
}

impl OpenAiClient {
    async fn read_stream(&self, mut resp: reqwest::Response, on_text: OnText<'_>) -> Result<Reply, ModelError> {
        let mut reply = StreamReply::default();
        let mut visible = Visible::default();
        let mut buf: Vec<u8> = Vec::new();
        'read: loop {
            let chunk = match tokio::time::timeout(self.idle, resp.chunk()).await {
                Err(_) => {
                    let secs = self.idle.as_secs();
                    let text = format!("the server sent nothing for {secs} s in the middle of a reply");
                    return Err(ModelError::new(ErrorKind::Unreachable, text));
                }
                Ok(Err(e)) => return Err(ModelError::new(ErrorKind::Other, self.scrub(&format!("the reply stream broke: {e}")))),
                Ok(Ok(None)) => break,
                Ok(Ok(Some(chunk))) => chunk,
            };
            buf.extend_from_slice(&chunk);
            while let Some(end) = buf.iter().position(|b| *b == b'\n') {
                let line: Vec<u8> = buf.drain(..=end).collect();
                let line = String::from_utf8_lossy(&line);
                let Some(data) = line.trim().strip_prefix("data:").map(str::trim) else { continue };
                if data == "[DONE]" {
                    break 'read;
                }
                let Ok(v) = serde_json::from_str::<Value>(data) else { continue };
                let piece = reply.push(&v).map_err(|e| ModelError::new(e.kind, self.scrub(&e.message)))?;
                let shown = visible.push(&piece);
                if !shown.is_empty() {
                    on_text(&shown);
                }
            }
        }
        let rest = visible.finish();
        if !rest.is_empty() {
            on_text(&rest);
        }
        reply.finish()
    }

    /// One model call with the retries. With `on_text` the reply is streamed when the server can.
    async fn call(&self, req: Request<'_>, on_text: Option<OnText<'_>>) -> Result<Reply, ModelError> {
        let url = format!("{}/chat/completions", self.base_url);
        // What this call has given up after a refusal; the client remembers it once a call succeeds that way.
        let mut drop_choice = self.no_tool_choice.load(Ordering::Relaxed);
        let mut drop_stream = self.no_stream.load(Ordering::Relaxed);
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let mut body = build_body(&self.model, &req, self.disable_thinking);
            let forced = body.get("tool_choice").is_some();
            if forced && drop_choice {
                body.as_object_mut().expect("body is an object").remove("tool_choice");
            }
            let streaming = on_text.is_some() && !drop_stream;
            if streaming {
                body["stream"] = json!(true);
                body["stream_options"] = json!({"include_usage": true});
            }
            let mut call = self.http.post(&url).json(&body);
            if !self.api_key.is_empty() {
                call = call.bearer_auth(&self.api_key);
            }
            let (err, retries) = match call.send().await {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    if (200..300).contains(&status) {
                        if forced && drop_choice {
                            self.no_tool_choice.store(true, Ordering::Relaxed);
                        }
                        if on_text.is_some() && drop_stream {
                            self.no_stream.store(true, Ordering::Relaxed);
                        }
                        let event_stream = resp
                            .headers()
                            .get(reqwest::header::CONTENT_TYPE)
                            .and_then(|v| v.to_str().ok())
                            .is_some_and(|t| t.contains("text/event-stream"));
                        if let Some(on_text) = on_text.filter(|_| streaming && event_stream) {
                            return self.read_stream(resp, on_text).await;
                        }
                        let text = resp.text().await.unwrap_or_default();
                        let v: Value = serde_json::from_str(&text)
                            .map_err(|e| ModelError::new(ErrorKind::Other, format!("the server reply is not JSON: {e}")))?;
                        let reply = parse_reply(&v)?;
                        if let Some(on_text) = on_text.filter(|_| !reply.content.is_empty()) {
                            on_text(&reply.content);
                        }
                        return Ok(reply);
                    }
                    let text = resp.text().await.unwrap_or_default();
                    let kind = classify(status, &text);
                    // A server that refuses the request itself may not know `tool_choice` or streaming:
                    // ask again without them, one at a time.
                    if matches!(status, 400 | 422) && kind == ErrorKind::Other {
                        if forced && !drop_choice {
                            drop_choice = true;
                            attempt -= 1;
                            continue;
                        }
                        if streaming {
                            drop_stream = true;
                            attempt -= 1;
                            continue;
                        }
                    }
                    let retries = match kind {
                        ErrorKind::RateLimited => self.rate_limit_retries,
                        ErrorKind::Other if status >= 500 => 2,
                        _ => 0,
                    };
                    (ModelError::new(kind, self.scrub(&short_error(status, &text))), retries)
                }
                Err(e) => {
                    let kind = if e.is_connect() || e.is_timeout() { ErrorKind::Unreachable } else { ErrorKind::Other };
                    (ModelError::new(kind, self.scrub(&format!("{url}: {e}"))), 2)
                }
            };
            if attempt > retries {
                return Err(err);
            }
            let wait = self.backoff.saturating_mul(1 << attempt.min(5)).min(Duration::from_secs(60));
            tokio::time::sleep(wait).await;
        }
    }
}

impl Model for OpenAiClient {
    async fn complete(&self, req: Request<'_>) -> Result<Reply, ModelError> {
        self.call(req, None).await
    }

    async fn stream(&self, req: Request<'_>, on_text: &(dyn Fn(&str) + Sync)) -> Result<Reply, ModelError> {
        self.call(req, Some(on_text)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ToolSpec;

    #[test]
    fn body_carries_messages_tools_and_the_thinking_switch() {
        let messages = [
            Message::system("sys"),
            Message::user("fix it"),
            Message::assistant("", vec![ToolCall { id: "c1".into(), name: "read_file".into(), args: r#"{"path":"a.py"}"#.into() }]),
            Message::tool("c1", "a.py lines 1-1 of 1\n1| x"),
        ];
        let tools = [ToolSpec::new("read_file", "Read a file", json!({"type": "object"}))];
        let req = Request { messages: &messages, tools: &tools, max_tokens: 512, temperature: 0.1, tool_choice: ToolChoice::Auto };
        let body = build_body("qwen", &req, true);
        assert_eq!(body["model"], "qwen");
        assert_eq!(body["max_tokens"], 512);
        assert_eq!(body["messages"][2]["tool_calls"][0]["function"]["arguments"], r#"{"path":"a.py"}"#);
        assert_eq!(body["messages"][3]["role"], "tool");
        assert_eq!(body["messages"][3]["tool_call_id"], "c1");
        assert_eq!(body["tools"][0]["function"]["name"], "read_file");
        assert_eq!(body["chat_template_kwargs"]["enable_thinking"], false);
        assert!(body.get("tool_choice").is_none());
        let forced = build_body("qwen", &Request { tool_choice: ToolChoice::Required, ..req }, false);
        assert_eq!(forced["tool_choice"], "required");
        let plain = build_body("qwen", &Request { tools: &[], tool_choice: ToolChoice::Required, ..req }, false);
        assert!(plain.get("tools").is_none() && plain.get("tool_choice").is_none());
        assert!(plain.get("chat_template_kwargs").is_none());
    }

    #[test]
    fn stream_chunks_are_joined_into_one_reply() {
        let mut r = StreamReply::default();
        let chunks = [
            json!({"choices": [{"delta": {"role": "assistant", "content": "Reading"}}]}),
            json!({"choices": [{"delta": {"content": " it."}}]}),
            json!({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]}),
            json!({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\":\"a.py\"}"}}]}}]}),
            json!({"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "run_tests", "arguments": ""}}]}}]}),
            json!({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            json!({"choices": [], "usage": {"prompt_tokens": 700, "completion_tokens": 30}}),
        ];
        let text: String = chunks.iter().map(|c| r.push(c).unwrap()).collect();
        assert_eq!(text, "Reading it.");
        let reply = r.finish().unwrap();
        assert_eq!(reply.content, "Reading it.");
        assert_eq!(reply.finish, Finish::ToolCalls);
        assert_eq!(reply.tool_calls.len(), 2);
        assert_eq!((reply.tool_calls[0].id.as_str(), reply.tool_calls[0].args.as_str()), ("c1", r#"{"path":"a.py"}"#));
        assert_eq!((reply.tool_calls[1].name.as_str(), reply.tool_calls[1].args.as_str()), ("run_tests", "{}"));
        assert_eq!(reply.usage, Usage { prompt_tokens: 700, completion_tokens: 30 });
    }

    #[test]
    fn a_stream_that_reuses_index_zero_or_ends_early_is_still_read() {
        let mut r = StreamReply::default();
        let first = json!({"index": 0, "id": "a", "function": {"name": "read_file", "arguments": "{}"}});
        let second = json!({"index": 0, "id": "b", "function": {"name": "search", "arguments": {"pattern": "x"}}});
        r.push(&json!({"choices": [{"delta": {"tool_calls": [first]}}]})).unwrap();
        r.push(&json!({"choices": [{"delta": {"tool_calls": [second]}}]})).unwrap();
        let reply = r.finish().unwrap();
        assert_eq!(reply.tool_calls.iter().map(|c| c.name.as_str()).collect::<Vec<_>>(), ["read_file", "search"]);
        assert_eq!(reply.tool_calls[1].args, r#"{"pattern":"x"}"#);
        // No finish reason: the reply was cut.
        assert_eq!(reply.finish, Finish::Length);

        let mut broken = StreamReply::default();
        let err = broken.push(&json!({"error": {"message": "maximum context length exceeded"}})).unwrap_err();
        assert_eq!(err.kind, ErrorKind::Context);
        assert!(StreamReply::default().finish().is_err());
    }

    #[test]
    fn streamed_text_hides_thinking_and_written_out_tool_calls() {
        let mut v = Visible::default();
        let pieces =
            ["<thi", "nk>plan", "ning</th", "ink>\n\nI will ", "read it. <", "b> <tool", "_call>{\"name\"", ": \"x\"}</tool_call> more"];
        let shown: String = pieces.iter().map(|p| v.push(p)).collect::<String>() + &v.finish();
        assert_eq!(shown, "I will read it. <b> ");

        let mut plain = Visible::default();
        let shown: String = ["  Done", ". 1 <", " 2"].iter().map(|p| plain.push(p)).collect::<String>() + &plain.finish();
        assert_eq!(shown, "Done. 1 < 2");
    }

    #[test]
    fn reply_with_tool_calls_is_parsed() {
        let v = json!({
            "choices": [{"finish_reason": "tool_calls", "message": {"content": null, "tool_calls": [
                {"id": "abc", "type": "function", "function": {"name": "edit_file", "arguments": "{\"path\":\"a\"}"}},
                {"id": "", "type": "function", "function": {"name": "run_tests", "arguments": {}}}
            ]}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 40}
        });
        let r = parse_reply(&v).unwrap();
        assert_eq!(r.finish, Finish::ToolCalls);
        assert_eq!(r.tool_calls.len(), 2);
        assert_eq!(r.tool_calls[0].id, "abc");
        assert_eq!(r.tool_calls[1].id, "call_1");
        assert_eq!(r.tool_calls[1].args, "{}");
        assert_eq!(r.usage, Usage { prompt_tokens: 900, completion_tokens: 40 });
    }

    #[test]
    fn thinking_text_is_removed_and_text_tool_calls_are_recovered() {
        let v = json!({"choices": [{"finish_reason": "stop", "message": {"content":
            "<think>hmm</think>I will read it.\n<tool_call>\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"a.py\"}}\n</tool_call>"}}]});
        let r = parse_reply(&v).unwrap();
        assert_eq!(r.content, "I will read it.");
        assert_eq!(r.tool_calls.len(), 1);
        assert_eq!(r.tool_calls[0].name, "read_file");
        assert_eq!(r.tool_calls[0].args, r#"{"path":"a.py"}"#);
    }

    #[test]
    fn errors_are_classified() {
        assert_eq!(classify(401, ""), ErrorKind::Auth);
        assert_eq!(classify(404, "model not found"), ErrorKind::ModelNotFound);
        assert_eq!(classify(429, ""), ErrorKind::RateLimited);
        assert_eq!(classify(400, "This model's maximum context length is 16384 tokens."), ErrorKind::Context);
        assert_eq!(classify(400, "bad request"), ErrorKind::Other);
        assert_eq!(short_error(400, r#"{"error":{"message":"nope"}}"#), "HTTP 400: nope");
    }
}
