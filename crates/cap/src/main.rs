//! `cap` — the command line of CAP.

mod chat;
mod config;
mod term;
mod tui;

use cap_core::agent::{Agent, Config, Quiet, Report, Sink, Status};
use cap_core::cancel::Cancel;
use cap_core::gate::{self, Gate};
use cap_core::mcp::McpHub;
use cap_core::openai::OpenAiClient;
use cap_core::perm::Mode;
use cap_core::sandbox::{self, Policy, Sandbox};
use cap_core::skills::{self, Skill};
use cap_core::tools::Profile;
use cap_core::workspace::Workspace;
use clap::{Args, Parser, Subcommand};
use config::{Env, Flags, Settings};
use serde_json::json;
use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use term::{Ask, Out, Paint, Screen, first_line};

const SETTINGS_HELP: &str = "A flag wins over an environment variable (CAP_PROVIDER, CAP_MODEL, CAP_BASE_URL, CAP_CONTEXT, \
CAP_API_KEY), which wins over the settings file. `cap config` shows the file's place and the settings in force. \
CAP_PLAIN=1 makes the conversation a line prompt in place of the window.";

#[derive(Parser)]
#[command(
    name = "cap",
    version,
    about = "CAP (Code As Planned): a coding agent that says done only with evidence",
    after_help = SETTINGS_HELP,
    args_conflicts_with_subcommands = true
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
    #[command(flatten)]
    common: Common,
}

#[derive(Subcommand)]
enum Command {
    /// Talk with the agent in a project folder (what `cap` alone does)
    Chat(Box<Common>),
    /// Run one task in a project folder and exit
    Exec(Box<ExecArgs>),
    /// List the model servers CAP knows by name
    Providers,
    /// Show the settings in force and where the settings file lives
    Config(Box<Common>),
    /// Set up, undo or show the isolation that commands run in
    Sandbox {
        #[command(subcommand)]
        action: SandboxAction,
    },
    /// Start the outside tool servers of the settings file and list their tools
    Mcp,
    /// List the skills the agent can use in a project folder
    Skills {
        /// The project folder
        #[arg(short = 'C', long = "dir", default_value = ".")]
        dir: PathBuf,
    },
}

/// What a conversation and a single run have in common.
#[derive(Args, Clone, Debug, Default)]
struct Common {
    /// The project folder (the current folder when left out)
    #[arg(short = 'C', long = "dir")]
    dir: Option<PathBuf>,
    /// The command that decides whether the work is done (found from the project files when left out)
    #[arg(long, conflicts_with = "no_test")]
    test: Option<String>,
    /// Run without a test command
    #[arg(long)]
    no_test: bool,
    /// read: look only. edit: change files and run tests. auto: also run other commands unasked
    #[arg(long)]
    mode: Option<String>,
    /// Model calls before a turn stops
    #[arg(long)]
    max_steps: Option<u32>,
    /// A name from `cap providers`
    #[arg(long)]
    provider: Option<String>,
    /// The model, in place of the provider's own
    #[arg(long)]
    model: Option<String>,
    /// An OpenAI-compatible endpoint, in place of the provider's own
    #[arg(long)]
    base_url: Option<String>,
    /// The model's context window in tokens
    #[arg(long)]
    context: Option<u32>,
    /// Show the model's text when a reply is complete, not while it arrives
    #[arg(long)]
    no_stream: bool,
    /// Make every reply a tool call (for servers that can require one)
    #[arg(long)]
    strict_tools: bool,
    /// The tool set: small, standard or full (picked from the context window when left out)
    #[arg(long)]
    tools: Option<String>,
    /// Isolation for commands: auto, on or off
    #[arg(long)]
    sandbox: Option<String>,
}

impl Common {
    fn flags(&self) -> Flags {
        Flags {
            provider: self.provider.clone(),
            model: self.model.clone(),
            base_url: self.base_url.clone(),
            context: self.context,
            mode: self.mode.clone(),
            max_steps: self.max_steps,
            no_stream: self.no_stream,
            strict_tools: self.strict_tools,
            tools: self.tools.clone(),
            sandbox: self.sandbox.clone(),
        }
    }
}

#[derive(Args)]
struct ExecArgs {
    /// What to do, in plain words
    task: String,
    #[command(flatten)]
    common: Common,
    /// Print one JSON object with the result and keep progress off the screen
    #[arg(long)]
    json: bool,
    /// Write the whole conversation to this file as JSON lines
    #[arg(long)]
    trace: Option<PathBuf>,
}

#[derive(Subcommand)]
enum SandboxAction {
    /// Let the isolation use a project folder (Windows; other systems need no set-up)
    Setup {
        /// The project folder
        #[arg(short = 'C', long = "dir", default_value = ".")]
        dir: PathBuf,
        /// A folder of tools the commands may read; repeat for more
        #[arg(long = "tool", value_name = "DIR")]
        tools: Vec<PathBuf>,
    },
    /// Take back what `setup` gave
    Undo {
        /// The project folder
        #[arg(short = 'C', long = "dir", default_value = ".")]
        dir: PathBuf,
    },
    /// Say what the isolation can do for a project folder
    Status {
        /// The project folder
        #[arg(short = 'C', long = "dir", default_value = ".")]
        dir: PathBuf,
    },
}

/// Everything a conversation or a run works with, set up once.
pub struct Ready {
    pub settings: Settings,
    key: String,
    pub ws: Workspace,
    pub test_cmd: Option<String>,
    pub sandbox: Sandbox,
    /// Why commands run without isolation, when they do and nobody asked for that.
    pub notice: Option<String>,
    pub skills: Vec<Skill>,
    pub hub: McpHub,
    /// Outside tool servers that did not start.
    pub warnings: Vec<String>,
}

impl Ready {
    pub fn client(&self) -> OpenAiClient {
        let p = &self.settings.provider;
        let mut client = OpenAiClient::new(p.base_url.as_str(), self.key.as_str(), p.model.as_str());
        client.disable_thinking = p.no_thinking;
        client
    }

    /// The tests decide in the modes that can change files.
    pub fn gate(&self) -> Gate {
        match &self.test_cmd {
            Some(cmd) if self.settings.mode != Mode::Read => Gate::command(cmd.as_str()),
            _ => Gate::None,
        }
    }

    pub fn config(&self) -> Config {
        let s = &self.settings;
        Config {
            mode: s.mode,
            context_window: s.provider.context,
            max_steps: s.max_steps,
            eager_tests: s.eager_tests,
            strict_tools: s.strict_tools,
            stream: s.stream,
            profile: s.profile,
            ..Config::default()
        }
    }

    pub fn profile(&self) -> Profile {
        self.settings.profile.unwrap_or_else(|| Profile::for_window(self.settings.provider.context))
    }

    pub fn mcp(&self) -> Option<&McpHub> {
        (!self.hub.is_empty()).then_some(&self.hub)
    }

    pub fn tests(&self) -> &str {
        self.test_cmd.as_deref().unwrap_or("none")
    }

    /// The lines a session opens with: what runs, where, and what could not be had.
    pub fn header(&self, paint: Paint) -> String {
        let s = &self.settings;
        let mut lines = vec![
            format!(
                "{} · {} {} · {} tokens · mode {} · tools {}",
                paint.bold(&format!("cap {}", cap_core::VERSION)),
                s.provider.name,
                s.provider.model,
                s.provider.context,
                s.mode.as_str(),
                self.profile().as_str()
            ),
            paint.dim(&format!("folder {} · tests: {} · isolation: {}", self.ws.root().display(), self.tests(), self.sandbox.name())),
        ];
        lines.extend(self.notes().iter().map(|note| paint.yellow(&format!("! {note}"))));
        lines.join("\n")
    }

    /// What the person should know before the work starts.
    pub fn notes(&self) -> Vec<String> {
        self.notice.iter().chain(&self.warnings).cloned().collect()
    }
}

async fn prepare(common: &Common, env: Env<'_>) -> Result<Ready, String> {
    let loaded = config::load(env)?;
    let settings = config::resolve(&common.flags(), env, &loaded.file)?;
    if settings.provider.model.is_empty() {
        return Err(format!("name a model for {} with --model, CAP_MODEL or the settings file", settings.provider.name));
    }
    let key = config::key(&settings.provider, env)?;
    let dir = common.dir.clone().unwrap_or_else(|| PathBuf::from("."));
    let ws = Workspace::new(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let test_cmd = match (&common.test, common.no_test) {
        (Some(cmd), _) => Some(cmd.clone()),
        (None, true) => None,
        (None, false) => gate::detect(ws.root()),
    };
    let policy = Policy::with_extras(ws.root(), &settings.sandbox_read, &settings.sandbox_write);
    let (sandbox, notice) = sandbox::choose(settings.sandbox, policy).await?;
    let skills = skills::load(ws.root(), config::home(env).as_deref());
    let (hub, warnings) = McpHub::start(&settings.mcp, ws.root()).await;
    Ok(Ready { settings, key, ws, test_cmd, sandbox, notice, skills, hub, warnings })
}

/// Ctrl-C stops the turn that is running. With no turn running, or pressed again, it ends CAP.
pub struct Interrupt {
    pub cancel: Arc<Cancel>,
    running: Arc<AtomicBool>,
}

impl Interrupt {
    fn listen() -> Interrupt {
        let interrupt = Interrupt { cancel: Arc::new(Cancel::new()), running: Arc::new(AtomicBool::new(false)) };
        let (cancel, running) = (interrupt.cancel.clone(), interrupt.running.clone());
        tokio::spawn(async move {
            while tokio::signal::ctrl_c().await.is_ok() {
                if running.load(Ordering::SeqCst) && !cancel.is_cancelled() {
                    cancel.cancel();
                } else {
                    eprintln!();
                    std::process::exit(130);
                }
            }
        });
        interrupt
    }

    pub fn begin(&self) {
        self.cancel.reset();
        self.running.store(true, Ordering::SeqCst);
    }

    pub fn end(&self) {
        self.running.store(false, Ordering::SeqCst);
    }
}

/// How a turn ended, in one line.
pub fn headline(status: &Status) -> String {
    match status {
        Status::Verified => "DONE — the tests pass".to_string(),
        Status::Unverified => "DONE, NOT VERIFIED — this project has no tests to check the work with".to_string(),
        Status::Answered => "ANSWERED".to_string(),
        Status::TestsFail => "NOT DONE — the tests still fail".to_string(),
        Status::StepLimit => "NOT DONE — the step limit was reached".to_string(),
        Status::Stuck(why) => format!("NOT DONE — {why}"),
        Status::ModelFailed(kind) => format!("NOT DONE — the model server failed ({kind:?})"),
        Status::ContextFull => "NOT DONE — the work does not fit in the model's context window".to_string(),
        Status::Interrupted => "STOPPED — interrupted before the work was done".to_string(),
    }
}

/// The headline in the colour of its news.
pub fn painted_headline(status: &Status, paint: Paint) -> String {
    let text = headline(status);
    match status {
        Status::Verified => paint.green(&text),
        Status::Answered => paint.dim(&text),
        Status::Unverified | Status::Interrupted => paint.yellow(&text),
        _ => paint.red(&text),
    }
}

fn exit_code(status: &Status) -> u8 {
    match status {
        Status::Verified | Status::Answered => 0,
        Status::Unverified => 3,
        Status::Interrupted => 130,
        _ => 1,
    }
}

fn detail(status: &Status) -> Option<String> {
    match status {
        Status::Stuck(why) => Some(why.clone()),
        Status::ModelFailed(kind) => Some(format!("{kind:?}")),
        _ => None,
    }
}

fn write_trace(path: &Path, report: &Report) -> std::io::Result<()> {
    let mut out = std::io::BufWriter::new(std::fs::File::create(path)?);
    for m in &report.transcript {
        let calls: Vec<_> = m.tool_calls.iter().map(|c| json!({"id": c.id, "name": c.name, "args": c.args})).collect();
        writeln!(out, "{}", json!({"role": m.role.as_str(), "content": m.content, "tool_calls": calls, "tool_call_id": m.tool_call_id}))?;
    }
    out.flush()
}

async fn exec(args: ExecArgs, env: Env<'_>) -> Result<ExitCode, String> {
    let ready = prepare(&args.common, env).await?;
    let err = Out::Stderr;
    let paint = Paint::for_out(err);
    if !args.json {
        err.line(&ready.header(paint));
    }
    let interrupt = Interrupt::listen();
    let client = ready.client();
    let gate = ready.gate();
    let screen = Screen::brief(err);
    let sink: &dyn Sink = if args.json { &Quiet } else { &screen };
    let ask = Ask { out: err };
    let agent = Agent {
        model: &client,
        ws: &ready.ws,
        gate: &gate,
        approver: &ask,
        sink,
        config: ready.config(),
        cancel: Some(&*interrupt.cancel),
        sandbox: &ready.sandbox,
        skills: &ready.skills,
        mcp: ready.mcp(),
    };
    interrupt.begin();
    let report = agent.run(&args.task).await;
    interrupt.end();

    if let Some(path) = &args.trace {
        write_trace(path, &report).map_err(|e| format!("{}: {e}", path.display()))?;
    }
    if args.json {
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
            "provider": ready.settings.provider.name,
            "model": client.model(),
            "mode": ready.settings.mode.as_str(),
            "test_command": ready.test_cmd,
            "sandbox": ready.sandbox.name(),
            "notes": ready.notes(),
        });
        Out::Stdout.line(&out.to_string());
    } else {
        err.line("");
        if !report.text.is_empty() {
            Out::Stdout.line(&report.text);
        }
        if let (Status::TestsFail, Some(tail)) = (&report.status, &report.failure) {
            err.line(tail);
        }
        err.line(&painted_headline(&report.status, paint));
        if !report.changed.is_empty() {
            err.line(&format!("changed: {}", report.changed.join(", ")));
        }
        err.line(&paint.dim(&format!(
            "{} steps · {:.1} s · {} tokens in, {} out · {} rejected \"done\"",
            report.steps, report.seconds, report.prompt_tokens, report.completion_tokens, report.rejections
        )));
    }
    Ok(ExitCode::from(exit_code(&report.status)))
}

/// Whether the conversation gets the window: a person at a terminal that can be drawn on, who did not ask for lines.
fn wants_window(env: Env<'_>) -> bool {
    env("CAP_PLAIN").is_none() && std::io::stdin().is_terminal() && Out::Stdout.is_terminal() && term::colour_codes_work(Out::Stdout)
}

async fn converse(common: &Common, env: Env<'_>) -> Result<ExitCode, String> {
    let ready = prepare(common, env).await?;
    if wants_window(env) {
        tui::run(ready, env).await.map_err(|e| format!("the terminal did not open the window ({e}); CAP_PLAIN=1 talks line by line"))?;
    } else {
        let interrupt = Interrupt::listen();
        chat::run(ready, &interrupt).await;
    }
    Ok(ExitCode::SUCCESS)
}

fn providers(env: Env<'_>) -> Result<ExitCode, String> {
    let loaded = config::load(env)?;
    let mut text = format!("{:<11} {:<14} {:>8}  {:<28} endpoint\n", "name", "model", "context", "key");
    for p in config::providers(&loaded.file)? {
        let model = if p.model.is_empty() { "--model" } else { &p.model };
        text.push_str(&format!("{:<11} {:<14} {:>8}  {:<28} {}\n", p.name, model, p.context, config::key_source(&p, env), p.base_url));
    }
    Out::Stdout.print(&text);
    Ok(ExitCode::SUCCESS)
}

fn show_config(common: &Common, env: Env<'_>) -> Result<ExitCode, String> {
    let loaded = config::load(env)?;
    let settings = config::resolve(&common.flags(), env, &loaded.file)?;
    Out::Stdout.line(&config::describe(&loaded, &settings, env));
    Ok(ExitCode::SUCCESS)
}

fn project(dir: &Path) -> Result<Workspace, String> {
    Workspace::new(dir).map_err(|e| format!("{}: {e}", dir.display()))
}

fn isolation(action: SandboxAction) -> Result<ExitCode, String> {
    let said = match action {
        SandboxAction::Setup { dir, tools } => sandbox::setup(project(&dir)?.root(), &tools)?,
        SandboxAction::Undo { dir } => sandbox::undo(project(&dir)?.root())?,
        SandboxAction::Status { dir } => sandbox::status(project(&dir)?.root()),
    };
    Out::Stdout.line(&said);
    Ok(ExitCode::SUCCESS)
}

/// The tools of the outside servers, one per line.
pub fn mcp_text(hub: &McpHub) -> String {
    if hub.is_empty() {
        return "no outside tool servers are running. The settings file adds one as [mcp.NAME] with a command.".to_string();
    }
    let mut lines = Vec::new();
    for server in hub.servers() {
        let how = if hub.tools().iter().any(|t| t.server == server && hub.allowed(&t.full) == Some(true)) {
            "runs without asking"
        } else {
            "asks before each call"
        };
        lines.push(format!("{server} ({how})"));
        for tool in hub.tools().iter().filter(|t| t.server == server) {
            lines.push(format!("  {}  {}", tool.full, first_line(&tool.description, 90)));
        }
    }
    lines.join("\n")
}

/// The skills, one per line.
pub fn skills_text(skills: &[Skill]) -> String {
    if skills.is_empty() {
        return format!("no skills here. A skill is a folder with a SKILL.md in {}.", skills::SKILL_DIRS.join(", "));
    }
    skills.iter().map(|s| format!("{}  {}  ({})", s.name, first_line(&s.description, 90), s.dir.display())).collect::<Vec<_>>().join("\n")
}

async fn outside_tools(env: Env<'_>) -> Result<ExitCode, String> {
    let loaded = config::load(env)?;
    let settings = config::resolve(&Flags::default(), env, &loaded.file)?;
    let here = std::env::current_dir().map_err(|e| format!("the current folder: {e}"))?;
    let (hub, warnings) = McpHub::start(&settings.mcp, &here).await;
    for warning in &warnings {
        Out::Stderr.line(&format!("! {warning}"));
    }
    Out::Stdout.line(&mcp_text(&hub));
    Ok(if warnings.is_empty() { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

fn list_skills(dir: &Path, env: Env<'_>) -> Result<ExitCode, String> {
    let ws = project(dir)?;
    Out::Stdout.line(&skills_text(&skills::load(ws.root(), config::home(env).as_deref())));
    Ok(ExitCode::SUCCESS)
}

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    let env: Env<'_> = &config::process_env;
    let done = match cli.command {
        None => converse(&cli.common, env).await,
        Some(Command::Chat(common)) => converse(&common, env).await,
        Some(Command::Exec(args)) => exec(*args, env).await,
        Some(Command::Providers) => providers(env),
        Some(Command::Config(common)) => show_config(&common, env),
        Some(Command::Sandbox { action }) => isolation(action),
        Some(Command::Mcp) => outside_tools(env).await,
        Some(Command::Skills { dir }) => list_skills(&dir, env),
    };
    done.unwrap_or_else(|message| {
        eprintln!("cap: {message}");
        ExitCode::from(2)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    fn cli(args: &[&str]) -> Result<Cli, clap::Error> {
        Cli::try_parse_from(std::iter::once("cap").chain(args.iter().copied()))
    }

    #[test]
    fn the_command_line_is_well_formed() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cap_alone_is_a_conversation_and_takes_the_same_flags_as_a_run() {
        let bare = cli(&[]).unwrap();
        assert!(bare.command.is_none() && bare.common.model.is_none());

        let with_flags = cli(&["--provider", "kau", "--mode", "read", "-C", "proj", "--no-stream"]).unwrap();
        assert!(with_flags.command.is_none());
        let flags = with_flags.common.flags();
        assert_eq!((flags.provider.as_deref(), flags.mode.as_deref(), flags.no_stream), (Some("kau"), Some("read"), true));
        assert_eq!(with_flags.common.dir, Some(PathBuf::from("proj")));

        let Some(Command::Chat(common)) = cli(&["chat", "--model", "m", "--sandbox", "off"]).unwrap().command else { panic!("chat") };
        assert_eq!((common.model.as_deref(), common.sandbox.as_deref()), (Some("m"), Some("off")));

        let Some(Command::Exec(run)) =
            cli(&["exec", "fix the bug", "--json", "--test", "pytest -q", "--tools", "small", "--context", "4096"]).unwrap().command
        else {
            panic!("exec")
        };
        assert_eq!((run.task.as_str(), run.json, run.common.test.as_deref()), ("fix the bug", true, Some("pytest -q")));
        assert_eq!((run.common.tools.as_deref(), run.common.context), (Some("small"), Some(4096)));
    }

    #[test]
    fn the_other_commands_read_their_own_arguments() {
        let Some(Command::Sandbox { action: SandboxAction::Setup { dir, tools } }) =
            cli(&["sandbox", "setup", "-C", "p", "--tool", "a", "--tool", "b"]).unwrap().command
        else {
            panic!("sandbox setup")
        };
        assert_eq!((dir, tools), (PathBuf::from("p"), vec![PathBuf::from("a"), PathBuf::from("b")]));
        assert!(matches!(cli(&["sandbox", "status"]).unwrap().command, Some(Command::Sandbox { action: SandboxAction::Status { .. } })));
        assert!(matches!(cli(&["providers"]).unwrap().command, Some(Command::Providers)));
        assert!(matches!(cli(&["mcp"]).unwrap().command, Some(Command::Mcp)));
        assert!(matches!(cli(&["skills"]).unwrap().command, Some(Command::Skills { .. })));
        assert!(matches!(cli(&["config", "--provider", "kau"]).unwrap().command, Some(Command::Config(_))));

        assert!(cli(&["exec", "task", "--test", "x", "--no-test"]).is_err());
        assert!(cli(&["exec"]).is_err());
        assert!(cli(&["--model", "m", "exec", "task"]).is_err(), "flags go after the command they belong to");
    }

    #[test]
    fn the_end_of_a_turn_has_a_line_and_an_exit_code() {
        use cap_core::model::ErrorKind;
        let all = [
            (Status::Verified, 0, "DONE — the tests pass"),
            (Status::Answered, 0, "ANSWERED"),
            (Status::Unverified, 3, "DONE, NOT VERIFIED"),
            (Status::TestsFail, 1, "NOT DONE — the tests still fail"),
            (Status::StepLimit, 1, "step limit"),
            (Status::Stuck("the same call failed 3 times".into()), 1, "the same call failed 3 times"),
            (Status::ModelFailed(ErrorKind::Auth), 1, "model server failed (Auth)"),
            (Status::ContextFull, 1, "context window"),
            (Status::Interrupted, 130, "STOPPED"),
        ];
        for (status, code, words) in all {
            assert_eq!(exit_code(&status), code, "{status:?}");
            assert!(headline(&status).contains(words), "{status:?}");
            assert_eq!(painted_headline(&status, Paint(false)), headline(&status));
        }
        assert_eq!(detail(&Status::Stuck("why".into())).as_deref(), Some("why"));
        assert_eq!(detail(&Status::Verified), None);
        assert!(painted_headline(&Status::Verified, Paint(true)).starts_with("\x1b[32m"));
    }
}
