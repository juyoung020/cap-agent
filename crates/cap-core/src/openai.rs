//! Client for OpenAI-compatible chat completion servers (vLLM, Ollama, LM Studio, llama.cpp, hosted APIs).
//!
//! `build_body` and `parse_reply` are pure so the wire format is tested without a network.

use crate::model::{ErrorKind, Finish, Message, Model, ModelError, Reply, Request, Role, ToolCall, Usage};
use serde_json::{Value, json};
use std::time::Duration;

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

impl Model for OpenAiClient {
    async fn complete(&self, req: Request<'_>) -> Result<Reply, ModelError> {
        let body = build_body(&self.model, &req, self.disable_thinking);
        let url = format!("{}/chat/completions", self.base_url);
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let mut call = self.http.post(&url).json(&body);
            if !self.api_key.is_empty() {
                call = call.bearer_auth(&self.api_key);
            }
            let (err, retries) = match call.send().await {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    let text = resp.text().await.unwrap_or_default();
                    if (200..300).contains(&status) {
                        let v: Value = serde_json::from_str(&text)
                            .map_err(|e| ModelError::new(ErrorKind::Other, format!("the server reply is not JSON: {e}")))?;
                        return parse_reply(&v);
                    }
                    let kind = classify(status, &text);
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
        let tools = [ToolSpec { name: "read_file", description: "Read a file", parameters: json!({"type": "object"}) }];
        let req = Request { messages: &messages, tools: &tools, max_tokens: 512, temperature: 0.1 };
        let body = build_body("qwen", &req, true);
        assert_eq!(body["model"], "qwen");
        assert_eq!(body["max_tokens"], 512);
        assert_eq!(body["messages"][2]["tool_calls"][0]["function"]["arguments"], r#"{"path":"a.py"}"#);
        assert_eq!(body["messages"][3]["role"], "tool");
        assert_eq!(body["messages"][3]["tool_call_id"], "c1");
        assert_eq!(body["tools"][0]["function"]["name"], "read_file");
        assert_eq!(body["chat_template_kwargs"]["enable_thinking"], false);
        let plain = build_body("qwen", &Request { tools: &[], ..req }, false);
        assert!(plain.get("tools").is_none());
        assert!(plain.get("chat_template_kwargs").is_none());
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
