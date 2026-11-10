//! The HTTP client against a local server that replays canned responses.

use cap_core::model::{ErrorKind, Finish, Message, Model, Request, ToolSpec};
use cap_core::openai::OpenAiClient;
use serde_json::{Value, json};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

/// Answer one request per canned response, then return the requests that came in.
async fn serve(responses: Vec<(u16, String)>) -> (String, JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/v1/", listener.local_addr().unwrap());
    let handle = tokio::spawn(async move {
        let mut seen = Vec::new();
        for (status, body) in responses {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf = Vec::new();
            let mut chunk = [0u8; 8192];
            loop {
                let n = sock.read(&mut chunk).await.unwrap();
                buf.extend_from_slice(&chunk[..n]);
                if let Some(head) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&buf[..head]).to_lowercase();
                    let len: usize =
                        headers.lines().find_map(|l| l.strip_prefix("content-length:")).map_or(0, |v| v.trim().parse().unwrap());
                    if buf.len() >= head + 4 + len {
                        break;
                    }
                }
                if n == 0 {
                    break;
                }
            }
            seen.push(String::from_utf8_lossy(&buf).into_owned());
            let reply = format!(
                "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            sock.write_all(reply.as_bytes()).await.unwrap();
            let _ = sock.shutdown().await;
        }
        seen
    });
    (url, handle)
}

fn client(url: &str) -> OpenAiClient {
    let mut c = OpenAiClient::new(url, "sk-test-key-123", "tiny-model");
    c.backoff = Duration::from_millis(5);
    c
}

fn tool() -> Vec<ToolSpec> {
    vec![ToolSpec {
        name: "read_file",
        description: "Read a file.",
        parameters: json!({"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    }]
}

fn body_of(request: &str) -> Value {
    serde_json::from_str(request.split_once("\r\n\r\n").unwrap().1).unwrap()
}

const TOOL_REPLY: &str = r#"{"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": null,
    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]}}],
    "usage": {"prompt_tokens": 321, "completion_tokens": 12}}"#;

#[tokio::test]
async fn a_busy_server_is_tried_again_and_the_reply_is_parsed() {
    let (url, server) = serve(vec![(429, r#"{"error": {"message": "rate limit"}}"#.into()), (200, TOOL_REPLY.into())]).await;
    let mut c = client(&url);
    c.disable_thinking = true;
    let msgs = [Message::system("sys"), Message::user("read a.py")];
    let tools = tool();
    let reply = c.complete(Request { messages: &msgs, tools: &tools, max_tokens: 777, temperature: 0.2 }).await.unwrap();

    assert_eq!(reply.finish, Finish::ToolCalls);
    assert_eq!((reply.tool_calls[0].name.as_str(), reply.tool_calls[0].args.as_str()), ("read_file", r#"{"path": "a.py"}"#));
    assert_eq!((reply.usage.prompt_tokens, reply.usage.completion_tokens), (321, 12));

    let seen = server.await.unwrap();
    assert_eq!(seen.len(), 2);
    let head = seen[1].to_lowercase();
    assert!(head.starts_with("post /v1/chat/completions "), "{}", &head[..60]);
    assert!(head.contains("authorization: bearer sk-test-key-123"));
    let body = body_of(&seen[1]);
    assert_eq!(body["model"], "tiny-model");
    assert_eq!(body["max_tokens"], 777);
    assert_eq!(body["messages"][1], json!({"role": "user", "content": "read a.py"}));
    assert_eq!(body["tools"][0]["function"]["name"], "read_file");
    assert_eq!(body["chat_template_kwargs"]["enable_thinking"], false);
}

#[tokio::test]
async fn a_text_reply_with_thinking_and_a_written_out_call_is_cleaned() {
    let content =
        "<think>the user wants the file</think>\n<tool_call>\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"a.py\"}}\n</tool_call>";
    let reply = json!({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}).to_string();
    let (url, server) = serve(vec![(200, reply)]).await;
    let msgs = [Message::user("read a.py")];
    let tools = tool();
    let reply = client(&url).complete(Request { messages: &msgs, tools: &tools, max_tokens: 100, temperature: 0.0 }).await.unwrap();
    assert_eq!(reply.tool_calls.len(), 1);
    assert_eq!(reply.tool_calls[0].name, "read_file");
    assert!(!reply.content.contains("think") && !reply.content.contains("tool_call"), "{}", reply.content);
    // A server that was not asked to skip thinking gets no extra field.
    assert!(body_of(&server.await.unwrap()[0]).get("chat_template_kwargs").is_none());
}

#[tokio::test]
async fn a_refused_key_is_reported_without_showing_the_key() {
    let (url, server) = serve(vec![(401, r#"{"error": {"message": "Incorrect API key provided: sk-test-key-123"}}"#.into())]).await;
    let msgs = [Message::user("hi")];
    let err = client(&url).complete(Request { messages: &msgs, tools: &[], max_tokens: 100, temperature: 0.0 }).await.unwrap_err();
    assert_eq!(err.kind, ErrorKind::Auth);
    assert!(!err.to_string().contains("sk-test-key-123") && !err.message.contains("sk-test-key-123"), "{err}");
    assert_eq!(server.await.unwrap().len(), 1);
}

#[tokio::test]
async fn a_missing_model_and_a_full_context_are_told_apart() {
    let (url, _s) = serve(vec![(404, r#"{"error": {"message": "The model `tiny-model` does not exist"}}"#.into())]).await;
    let msgs = [Message::user("hi")];
    let req = Request { messages: &msgs, tools: &[], max_tokens: 100, temperature: 0.0 };
    assert_eq!(client(&url).complete(req).await.unwrap_err().kind, ErrorKind::ModelNotFound);

    let full = r#"{"error": {"message": "This model's maximum context length is 16384 tokens. However, you requested 20000 tokens"}}"#;
    let (url, _s) = serve(vec![(400, full.into())]).await;
    assert_eq!(client(&url).complete(req).await.unwrap_err().kind, ErrorKind::Context);
}

#[tokio::test]
async fn a_server_that_is_not_there_is_unreachable() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/v1", listener.local_addr().unwrap());
    drop(listener);
    let msgs = [Message::user("hi")];
    let err = client(&url).complete(Request { messages: &msgs, tools: &[], max_tokens: 100, temperature: 0.0 }).await.unwrap_err();
    assert_eq!(err.kind, ErrorKind::Unreachable);
}

#[tokio::test]
async fn a_server_that_stays_busy_gives_up_as_rate_limited() {
    let busy = (429, r#"{"error": {"message": "slow down"}}"#.to_string());
    let (url, server) = serve(vec![busy.clone(), busy.clone(), busy]).await;
    let mut c = client(&url);
    c.rate_limit_retries = 2;
    let msgs = [Message::user("hi")];
    let err = c.complete(Request { messages: &msgs, tools: &[], max_tokens: 100, temperature: 0.0 }).await.unwrap_err();
    assert_eq!(err.kind, ErrorKind::RateLimited);
    assert_eq!(server.await.unwrap().len(), 3);
}
