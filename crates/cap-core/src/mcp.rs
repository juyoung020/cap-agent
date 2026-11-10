//! A client for MCP servers that speak JSON-RPC over stdin and stdout.
//!
//! Each configured server is started once, asked for its tools, and its tools are offered to
//! the model under the name `mcp__<server>__<tool>`. The protocol code works on any pair of
//! byte streams, so the tests drive it through an in-memory pipe.

use crate::exec::clip;
use crate::model::ToolSpec;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;

pub const PROTOCOL_VERSION: &str = "2025-06-18";
const START_TIMEOUT: Duration = Duration::from_secs(20);
const CALL_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_PAGES: usize = 20;
const MAX_TOOLS_PER_SERVER: usize = 200;
const DESCRIPTION_CHARS: usize = 300;
/// Function names longer than this are refused by OpenAI-compatible servers.
const MAX_NAME: usize = 64;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ServerConfig {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    /// Calls to this server run without asking.
    pub allow: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct McpTool {
    pub server: String,
    /// The name the server knows the tool by.
    pub name: String,
    /// The name the model calls: `mcp__<server>__<tool>`.
    pub full: String,
    pub description: String,
    pub schema: Value,
}

type Reader = Box<dyn AsyncBufRead + Send + Unpin>;
type Writer = Box<dyn AsyncWrite + Send + Unpin>;

struct Wire {
    reader: Reader,
    writer: Writer,
    next_id: u64,
}

/// One connection to a server.
pub struct Client {
    wire: Mutex<Wire>,
    /// Kept so the process ends when the client is dropped.
    _child: Option<tokio::process::Child>,
}

async fn send(writer: &mut Writer, message: &Value) -> Result<(), String> {
    let mut line = message.to_string();
    line.push('\n');
    writer.write_all(line.as_bytes()).await.map_err(|e| format!("the server stopped reading ({e})"))?;
    writer.flush().await.map_err(|e| format!("the server stopped reading ({e})"))
}

impl Client {
    pub fn over(reader: Reader, writer: Writer) -> Client {
        Client { wire: Mutex::new(Wire { reader, writer, next_id: 1 }), _child: None }
    }

    async fn exchange(wire: &mut Wire, method: &str, params: Value) -> Result<Value, String> {
        let id = wire.next_id;
        wire.next_id += 1;
        send(&mut wire.writer, &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})).await?;
        let mut line = String::new();
        loop {
            line.clear();
            let n = wire.reader.read_line(&mut line).await.map_err(|e| format!("reading from the server failed ({e})"))?;
            if n == 0 {
                return Err("the server closed the connection".into());
            }
            let Ok(message) = serde_json::from_str::<Value>(line.trim()) else { continue };
            match (message.get("method").and_then(Value::as_str), message.get("id")) {
                // A request from the server: answer it so the server does not wait.
                (Some(asked), Some(their_id)) => {
                    let reply = if asked == "ping" {
                        json!({"jsonrpc": "2.0", "id": their_id, "result": {}})
                    } else {
                        json!({"jsonrpc": "2.0", "id": their_id, "error": {"code": -32601, "message": "method not found"}})
                    };
                    send(&mut wire.writer, &reply).await?;
                }
                // A notification.
                (Some(_), None) => {}
                (None, Some(got)) if got.as_u64() == Some(id) => {
                    if let Some(error) = message.get("error") {
                        let text = error.get("message").and_then(Value::as_str).unwrap_or("unknown error");
                        return Err(format!("the server answered with an error: {text}"));
                    }
                    return Ok(message.get("result").cloned().unwrap_or(Value::Null));
                }
                // The answer to a request that was given up on.
                _ => {}
            }
        }
    }

    async fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let mut wire = self.wire.lock().await;
        match tokio::time::timeout(timeout, Self::exchange(&mut wire, method, params)).await {
            Ok(result) => result,
            Err(_) => Err(format!("no answer within {} s", timeout.as_secs())),
        }
    }

    pub async fn initialize(&self) -> Result<(), String> {
        let params = json!({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cap", "version": crate::VERSION},
        });
        self.request("initialize", params, START_TIMEOUT).await?;
        let mut wire = self.wire.lock().await;
        send(&mut wire.writer, &json!({"jsonrpc": "2.0", "method": "notifications/initialized"})).await
    }

    /// Every tool of the server as (name, description, input schema).
    pub async fn list_tools(&self) -> Result<Vec<(String, String, Value)>, String> {
        let mut tools = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..MAX_PAGES {
            let params = cursor.as_ref().map_or_else(|| json!({}), |c| json!({"cursor": c}));
            let page = self.request("tools/list", params, START_TIMEOUT).await?;
            for tool in page.get("tools").and_then(Value::as_array).into_iter().flatten() {
                let Some(name) = tool.get("name").and_then(Value::as_str) else { continue };
                let description = tool.get("description").and_then(Value::as_str).unwrap_or("").to_string();
                let schema = match tool.get("inputSchema") {
                    Some(s) if s.is_object() => s.clone(),
                    _ => json!({"type": "object", "properties": {}}),
                };
                tools.push((name.to_string(), description, schema));
            }
            cursor = page.get("nextCursor").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
            if cursor.is_none() || tools.len() >= MAX_TOOLS_PER_SERVER {
                break;
            }
        }
        tools.truncate(MAX_TOOLS_PER_SERVER);
        Ok(tools)
    }

    /// Call a tool. `Err` holds the text of a failed call.
    pub async fn call_tool(&self, name: &str, args: &Map<String, Value>) -> Result<String, String> {
        let result = self.request("tools/call", json!({"name": name, "arguments": args}), CALL_TIMEOUT).await?;
        let mut parts: Vec<String> = Vec::new();
        for item in result.get("content").and_then(Value::as_array).into_iter().flatten() {
            match item.get("type").and_then(Value::as_str) {
                Some("text") => parts.push(item.get("text").and_then(Value::as_str).unwrap_or("").to_string()),
                Some(other) => parts.push(format!("[{other} content]")),
                None => {}
            }
        }
        if parts.is_empty()
            && let Some(structured) = result.get("structuredContent")
        {
            parts.push(structured.to_string());
        }
        let text = clip(parts.join("\n").trim(), 2000, 4000);
        let text = if text.is_empty() { "(no output)".to_string() } else { text };
        if result.get("isError").and_then(Value::as_bool).unwrap_or(false) { Err(text) } else { Ok(text) }
    }
}

/// The full path of a program named without one. On Windows this finds launchers such as
/// `npx.cmd`, which a bare name does not start.
fn resolve_program(command: &str) -> String {
    if !cfg!(windows) || command.contains(['/', '\\']) || Path::new(command).extension().is_some() {
        return command.to_string();
    }
    let Some(path) = std::env::var_os("PATH") else { return command.to_string() };
    for dir in std::env::split_paths(&path) {
        for ext in ["exe", "cmd", "bat", "com"] {
            let candidate = dir.join(format!("{command}.{ext}"));
            if candidate.is_file() {
                return candidate.to_string_lossy().into_owned();
            }
        }
    }
    command.to_string()
}

fn spawn(config: &ServerConfig, cwd: &Path) -> Result<Client, String> {
    let mut command = tokio::process::Command::new(resolve_program(&config.command));
    command.args(&config.args).envs(&config.env).current_dir(cwd);
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
    let mut child = command.spawn().map_err(|e| format!("cannot start `{}` ({e})", config.command))?;
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        return Err(format!("cannot talk to `{}`", config.command));
    };
    let mut client = Client::over(Box::new(BufReader::new(stdout)), Box::new(stdin));
    client._child = Some(child);
    Ok(client)
}

fn safe(name: &str) -> String {
    name.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '_' }).collect()
}

fn short(text: &str, max: usize) -> String {
    let one_line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if one_line.chars().count() <= max { one_line } else { format!("{}...", one_line.chars().take(max - 3).collect::<String>()) }
}

struct Server {
    allow: bool,
    client: Client,
}

/// The running servers and their tools.
#[derive(Default)]
pub struct McpHub {
    servers: BTreeMap<String, Server>,
    tools: Vec<McpTool>,
}

impl McpHub {
    /// Start every configured server. A server that fails to start is left out and reported
    /// in the returned warnings; the others still work.
    pub async fn start(configs: &[ServerConfig], cwd: &Path) -> (McpHub, Vec<String>) {
        let mut hub = McpHub::default();
        let mut warnings = Vec::new();
        for config in configs {
            let added = match spawn(config, cwd) {
                Ok(client) => hub.add(&config.name, config.allow, client).await,
                Err(e) => Err(e),
            };
            if let Err(e) = added {
                warnings.push(format!("MCP server {}: {e}", config.name));
            }
        }
        (hub, warnings)
    }

    /// Greet a server over an open connection and take in its tools.
    pub async fn add(&mut self, name: &str, allow: bool, client: Client) -> Result<usize, String> {
        let server = safe(name);
        if server.is_empty() || self.servers.contains_key(&server) {
            return Err("the server name is empty or used twice".into());
        }
        client.initialize().await?;
        let listed = client.list_tools().await?;
        let mut count = 0;
        for (tool, description, schema) in listed {
            let full: String = format!("mcp__{server}__{}", safe(&tool)).chars().take(MAX_NAME).collect();
            if self.tools.iter().any(|t| t.full == full) {
                continue;
            }
            self.tools.push(McpTool {
                server: server.clone(),
                name: tool,
                full,
                description: short(&description, DESCRIPTION_CHARS),
                schema,
            });
            count += 1;
        }
        self.servers.insert(server, Server { allow, client });
        Ok(count)
    }

    pub fn is_empty(&self) -> bool {
        self.tools.is_empty()
    }

    pub fn tools(&self) -> &[McpTool] {
        &self.tools
    }

    pub fn servers(&self) -> impl Iterator<Item = &str> {
        self.servers.keys().map(String::as_str)
    }

    pub fn specs(&self) -> Vec<ToolSpec> {
        self.tools.iter().map(|t| ToolSpec::new(t.full.clone(), t.description.clone(), t.schema.clone())).collect()
    }

    fn find(&self, full: &str) -> Option<(&McpTool, &Server)> {
        let tool = self.tools.iter().find(|t| t.full == full)?;
        Some((tool, self.servers.get(&tool.server)?))
    }

    /// Whether calls to this tool run without asking; `None` when there is no such tool.
    pub fn allowed(&self, full: &str) -> Option<bool> {
        self.find(full).map(|(_, server)| server.allow)
    }

    pub async fn call(&self, full: &str, args: &Map<String, Value>) -> Result<String, String> {
        let Some((tool, server)) = self.find(full) else { return Err(format!("There is no MCP tool named {full}.")) };
        server.client.call_tool(&tool.name, args).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::DuplexStream;

    /// A server with two pages of tools that also sends a notification and a request of its
    /// own before each answer.
    async fn fake_server(stream: DuplexStream) {
        let (reader, mut writer) = tokio::io::split(stream);
        let mut lines = BufReader::new(reader).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let message: Value = serde_json::from_str(&line).unwrap();
            let (Some(method), Some(id)) = (message["method"].as_str(), message.get("id")) else { continue };
            let result = match method {
                "initialize" => {
                    assert_eq!(message["params"]["clientInfo"]["name"], "cap");
                    Ok(
                        json!({"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}),
                    )
                }
                "tools/list" if message["params"]["cursor"].is_null() => Ok(json!({
                    "tools": [{"name": "echo", "description": "Echo  the\ntext back.",
                               "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}],
                    "nextCursor": "page-2",
                })),
                "tools/list" => Ok(json!({"tools": [{"name": "break it"}]})),
                "tools/call" => match message["params"]["name"].as_str().unwrap() {
                    "echo" => Ok(
                        json!({"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}, {"type": "image", "data": ""}]}),
                    ),
                    "break it" => Ok(json!({"content": [{"type": "text", "text": "it broke"}], "isError": true})),
                    _ => Err("unknown tool"),
                },
                _ => Err("method not found"),
            };
            let reply = match result {
                Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
                Err(text) => json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": text}}),
            };
            let noise = [
                json!({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}),
                json!({"jsonrpc": "2.0", "id": 900, "method": "roots/list"}),
                json!({"jsonrpc": "2.0", "id": 901, "method": "ping"}),
            ];
            for message in noise.iter().chain([&reply]) {
                writer.write_all(format!("{message}\n").as_bytes()).await.unwrap();
            }
        }
    }

    fn connect() -> Client {
        let (ours, theirs) = tokio::io::duplex(64 * 1024);
        tokio::spawn(fake_server(theirs));
        let (reader, writer) = tokio::io::split(ours);
        Client::over(Box::new(BufReader::new(reader)), Box::new(writer))
    }

    fn args(text: &str) -> Map<String, Value> {
        json!({"text": text}).as_object().unwrap().clone()
    }

    #[tokio::test]
    async fn tools_are_listed_across_pages_and_called_by_their_prefixed_name() {
        let mut hub = McpHub::default();
        assert!(hub.is_empty());
        assert_eq!(hub.add("my server", false, connect()).await, Ok(2));
        let names: Vec<&str> = hub.tools().iter().map(|t| t.full.as_str()).collect();
        assert_eq!(names, ["mcp__my_server__echo", "mcp__my_server__break_it"]);
        let specs = hub.specs();
        assert_eq!(specs[0].description, "Echo the text back.");
        assert_eq!(specs[0].parameters["required"][0], "text");
        assert_eq!(specs[1].parameters, json!({"type": "object", "properties": {}}));

        assert_eq!(hub.call("mcp__my_server__echo", &args("hello")).await, Ok("hello\n[image content]".to_string()));
        // The server knows the tool by its own name, spaces included.
        assert_eq!(hub.call("mcp__my_server__break_it", &Map::new()).await, Err("it broke".to_string()));
        assert!(hub.call("mcp__my_server__nope", &Map::new()).await.unwrap_err().contains("no MCP tool"));
        assert_eq!(hub.allowed("mcp__my_server__echo"), Some(false));
        assert_eq!(hub.allowed("mcp__other__echo"), None);
    }

    #[tokio::test]
    async fn a_second_server_keeps_its_own_permission_and_names_stay_unique() {
        let mut hub = McpHub::default();
        hub.add("a", false, connect()).await.unwrap();
        hub.add("b", true, connect()).await.unwrap();
        assert!(hub.add("a", true, connect()).await.is_err());
        assert_eq!(hub.servers().collect::<Vec<_>>(), ["a", "b"]);
        assert_eq!((hub.allowed("mcp__a__echo"), hub.allowed("mcp__b__echo")), (Some(false), Some(true)));
        assert_eq!(hub.call("mcp__b__echo", &args("x")).await.unwrap(), "x\n[image content]");
    }

    #[tokio::test]
    async fn a_server_that_goes_away_or_cannot_start_is_an_error_not_a_hang() {
        let (ours, theirs) = tokio::io::duplex(1024);
        drop(theirs);
        let (reader, writer) = tokio::io::split(ours);
        let client = Client::over(Box::new(BufReader::new(reader)), Box::new(writer));
        assert!(client.initialize().await.is_err());

        let config = ServerConfig { name: "ghost".into(), command: "cap-no-such-program-here".into(), ..ServerConfig::default() };
        let (hub, warnings) = McpHub::start(&[config], &std::env::temp_dir()).await;
        assert!(hub.is_empty());
        assert!(warnings.len() == 1 && warnings[0].starts_with("MCP server ghost: cannot start"));
    }
}
