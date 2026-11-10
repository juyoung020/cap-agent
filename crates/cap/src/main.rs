//! `cap` — the command line of CAP.

use cap_core::agent::{Agent, Config, Event, Report, Sink, Status};
use cap_core::gate::{self, Gate, Verdict};
use cap_core::model::Role;
use cap_core::openai::OpenAiClient;
use cap_core::perm::{Approver, Mode};
use cap_core::tools::Status as ToolStatus;
use cap_core::workspace::Workspace;
use clap::{Parser, Subcommand};
use serde_json::json;
use std::io::{IsTerminal, Write};
use std::path::PathBuf;
use std::process::ExitCode;

struct Provider {
    name: &'static str,
    base_url: &'static str,
    /// The environment variable that holds the key; empty for servers that need none.
    key_env: &'static str,
    /// The model used when none is named.
    model: &'static str,
    context: u32,
    /// The server needs to be told to skip thinking output.
    no_thinking: bool,
}

const PROVIDERS: &[Provider] = &[
    Provider { name: "ollama", base_url: "http://localhost:11434/v1", key_env: "", model: "qwen3:8b", context: 8192, no_thinking: false },
    Provider { name: "lmstudio", base_url: "http://localhost:1234/v1", key_env: "", model: "", context: 8192, no_thinking: false },
    Provider {
        name: "kau",
        base_url: "https://agent.kau.ac.kr/v1",
        key_env: "KAU_API_KEY",
        model: "qwen3.5-9b",
        context: 16_384,
        no_thinking: true,
    },
    Provider {
        name: "openai",
        base_url: "https://api.openai.com/v1",
        key_env: "OPENAI_API_KEY",
        model: "",
        context: 128_000,
        no_thinking: false,
    },
    Provider {
        name: "anthropic",
        base_url: "https://api.anthropic.com/v1",
        key_env: "ANTHROPIC_API_KEY",
        model: "",
        context: 200_000,
        no_thinking: false,
    },
    Provider {
        name: "gemini",
        base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
        key_env: "GEMINI_API_KEY",
        model: "",
        context: 128_000,
        no_thinking: false,
    },
    Provider {
        name: "openrouter",
        base_url: "https://openrouter.ai/api/v1",
        key_env: "OPENROUTER_API_KEY",
        model: "",
        context: 128_000,
        no_thinking: false,
    },
];

#[derive(Parser)]
#[command(name = "cap", version, about = "CAP (Code As Planned): a coding agent that says done only with evidence")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run one task in a project folder and exit
    Exec(Box<ExecArgs>),
    /// List the model servers CAP knows by name
    Providers,
}

#[derive(clap::Args)]
struct ExecArgs {
    /// What to do, in plain words
    task: String,
    /// The project folder
    #[arg(short = 'C', long = "dir", default_value = ".")]
    dir: PathBuf,
    /// The command that decides whether the work is done (found from the project files when left out)
    #[arg(long, conflicts_with = "no_test")]
    test: Option<String>,
    /// Run without a test command
    #[arg(long)]
    no_test: bool,
    /// read: look only. edit: change files and run tests. auto: also run other commands unasked
    #[arg(long, default_value = "edit")]
    mode: String,
    /// Model calls before the run stops
    #[arg(long, default_value_t = 30)]
    max_steps: u32,
    /// Print one JSON object with the result and keep progress off the screen
    #[arg(long)]
    json: bool,
    /// A name from `cap providers`
    #[arg(long, env = "CAP_PROVIDER", default_value = "ollama")]
    provider: String,
    #[arg(long, env = "CAP_MODEL")]
    model: Option<String>,
    /// An OpenAI-compatible endpoint, in place of the provider's own
    #[arg(long, env = "CAP_BASE_URL")]
    base_url: Option<String>,
    /// The model's context window in tokens
    #[arg(long, env = "CAP_CONTEXT")]
    context: Option<u32>,
    /// Write the whole conversation to this file as JSON lines
    #[arg(long)]
    trace: Option<PathBuf>,
}

/// Progress on the terminal, one short line per event.
struct Terminal;

fn first_line(text: &str, max: usize) -> String {
    let line = text.lines().find(|l| !l.trim().is_empty()).unwrap_or("").trim();
    if line.chars().count() > max { line.chars().take(max).collect::<String>() + "…" } else { line.to_string() }
}

impl Sink for Terminal {
    fn event(&self, event: &Event<'_>) {
        match event {
            Event::Step(_) => {}
            Event::Text(text) => eprintln!("  {}", first_line(text, 160)),
            Event::Call { name, args } => eprintln!("> {name} {}", first_line(args, 140)),
            Event::Result { status, text, .. } => {
                let mark = match status {
                    ToolStatus::Ok => "ok",
                    ToolStatus::Failed => "failed",
                    ToolStatus::Denied => "refused",
                };
                eprintln!("  [{mark}] {}", first_line(text, 140));
            }
            Event::Gate(verdict) => match verdict {
                Verdict::Pass => eprintln!("= tests pass"),
                Verdict::NoTests => eprintln!("= no tests found"),
                Verdict::Fail { tail, .. } => eprintln!("= tests fail: {}", first_line(tail.lines().last().unwrap_or(""), 120)),
            },
            Event::Nudge(text) => eprintln!("! {}", first_line(text, 140)),
            Event::Compacted { stubbed, dropped } => eprintln!("~ context trimmed ({stubbed} shortened, {dropped} dropped)"),
        }
    }
}

/// Asks on the terminal; with no terminal there is nobody to ask, so the answer is no.
struct Ask;

impl Approver for Ask {
    fn approve(&self, action: &str, why: &str) -> bool {
        let stdin = std::io::stdin();
        if !stdin.is_terminal() || !std::io::stderr().is_terminal() {
            return false;
        }
        eprint!("? {action}\n  ({why}) allow? [y/N] ");
        let _ = std::io::stderr().flush();
        let mut line = String::new();
        stdin.read_line(&mut line).is_ok() && matches!(line.trim().to_lowercase().as_str(), "y" | "yes")
    }
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

fn providers() {
    println!("{:<11} {:<12} {:>8}  {:<19} endpoint", "name", "model", "context", "key");
    for p in PROVIDERS {
        let key = match p.key_env {
            "" => "none needed".to_string(),
            name if env(name).is_some() => format!("{name} (set)"),
            name => name.to_string(),
        };
        let model = if p.model.is_empty() { "--model" } else { p.model };
        println!("{:<11} {:<12} {:>8}  {:<19} {}", p.name, model, p.context, key, p.base_url);
    }
}

fn detail(status: &Status) -> Option<String> {
    match status {
        Status::Stuck(why) => Some(why.clone()),
        Status::ModelFailed(kind) => Some(format!("{kind:?}")),
        _ => None,
    }
}

fn write_trace(path: &PathBuf, report: &Report) -> std::io::Result<()> {
    let mut out = std::io::BufWriter::new(std::fs::File::create(path)?);
    for m in &report.transcript {
        let role = match m.role {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
        };
        let calls: Vec<_> = m.tool_calls.iter().map(|c| json!({"id": c.id, "name": c.name, "args": c.args})).collect();
        writeln!(out, "{}", json!({"role": role, "content": m.content, "tool_calls": calls, "tool_call_id": m.tool_call_id}))?;
    }
    out.flush()
}

async fn exec(a: ExecArgs) -> Result<ExitCode, String> {
    let Some(mode) = Mode::parse(&a.mode) else {
        return Err(format!("--mode is read, edit or auto (got {})", a.mode));
    };
    let Some(p) = PROVIDERS.iter().find(|p| p.name == a.provider.to_lowercase()) else {
        let names: Vec<_> = PROVIDERS.iter().map(|p| p.name).collect();
        return Err(format!("no provider named {}. The names are: {}", a.provider, names.join(", ")));
    };
    let model = a.model.clone().or_else(|| (!p.model.is_empty()).then(|| p.model.to_string()));
    let Some(model) = model else {
        return Err(format!("name a model for {} with --model or CAP_MODEL", p.name));
    };
    let key = match env("CAP_API_KEY").or_else(|| if p.key_env.is_empty() { None } else { env(p.key_env) }) {
        Some(key) => key,
        None if p.key_env.is_empty() => "local".to_string(),
        None => return Err(format!("{} needs a key: set {} or CAP_API_KEY", p.name, p.key_env)),
    };
    let base_url = a.base_url.clone().unwrap_or_else(|| p.base_url.to_string());
    let mut client = OpenAiClient::new(base_url, key, model);
    client.disable_thinking = p.no_thinking;

    let ws = Workspace::new(&a.dir).map_err(|e| format!("{}: {e}", a.dir.display()))?;
    let test_cmd = match (&a.test, a.no_test || mode == Mode::Read) {
        (Some(cmd), _) => Some(cmd.clone()),
        (None, true) => None,
        (None, false) => gate::detect(ws.root()),
    };
    let gate = test_cmd.as_deref().map_or(Gate::None, Gate::command);
    let config = Config { mode, context_window: a.context.unwrap_or(p.context), max_steps: a.max_steps, ..Config::default() };

    if !a.json {
        eprintln!("cap {} · {} {} · {} tokens · mode {}", cap_core::VERSION, p.name, client.model(), config.context_window, mode.as_str());
        eprintln!("folder {} · tests: {}", ws.root().display(), test_cmd.as_deref().unwrap_or("none"));
    }
    let quiet = cap_core::agent::Quiet;
    let sink: &dyn Sink = if a.json { &quiet } else { &Terminal };
    let agent = Agent { model: &client, ws: &ws, gate: &gate, approver: &Ask, sink, config };
    let report = agent.run(&a.task).await;

    if let Some(path) = &a.trace {
        write_trace(path, &report).map_err(|e| format!("{}: {e}", path.display()))?;
    }
    if a.json {
        let out = json!({
            "status": report.status.as_str(),
            "detail": detail(&report.status),
            "text": report.text,
            "steps": report.steps,
            "prompt_tokens": report.prompt_tokens,
            "completion_tokens": report.completion_tokens,
            "seconds": (report.seconds * 10.0).round() / 10.0,
            "changed": report.changed,
            "rejections": report.rejections,
            "failure": report.failure,
            "provider": p.name,
            "model": client.model(),
            "test_command": test_cmd,
        });
        println!("{out}");
    } else {
        let headline = match &report.status {
            Status::Verified => "DONE — the tests pass".to_string(),
            Status::Unverified => "DONE, NOT VERIFIED — this project has no tests to check the work with".to_string(),
            Status::Answered => "ANSWERED".to_string(),
            Status::TestsFail => "NOT DONE — the tests still fail".to_string(),
            Status::StepLimit => "NOT DONE — the step limit was reached".to_string(),
            Status::Stuck(why) => format!("NOT DONE — {why}"),
            Status::ModelFailed(_) => "NOT DONE — the model server failed".to_string(),
            Status::ContextFull => "NOT DONE — the work does not fit in the model's context window".to_string(),
        };
        eprintln!();
        if !report.text.is_empty() {
            println!("{}", report.text);
        }
        if let (Status::TestsFail, Some(tail)) = (&report.status, &report.failure) {
            eprintln!("{tail}");
        }
        eprintln!("{headline}");
        if !report.changed.is_empty() {
            eprintln!("changed: {}", report.changed.join(", "));
        }
        eprintln!(
            "{} steps · {:.1} s · {} tokens in, {} out · {} rejected \"done\"",
            report.steps, report.seconds, report.prompt_tokens, report.completion_tokens, report.rejections
        );
    }
    Ok(match report.status {
        Status::Verified | Status::Answered => ExitCode::SUCCESS,
        Status::Unverified => ExitCode::from(3),
        _ => ExitCode::from(1),
    })
}

#[tokio::main]
async fn main() -> ExitCode {
    match Cli::parse().command {
        Command::Providers => {
            providers();
            ExitCode::SUCCESS
        }
        Command::Exec(args) => exec(*args).await.unwrap_or_else(|message| {
            eprintln!("cap: {message}");
            ExitCode::from(2)
        }),
    }
}
