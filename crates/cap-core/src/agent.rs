//! The agent loop. The model proposes a step; the code here runs it, judges it and decides
//! when the work counts as finished.
//!
//! An [`Agent`] holds what stays the same (model, project, test gate, settings). A [`Session`]
//! holds one conversation and runs it turn by turn; `Agent::run` is a session with one turn.

use crate::board::{Board, Tests};
use crate::cancel::{self, Cancel};
use crate::codemap;
use crate::context::{self, Calibration};
use crate::edit::{self, Eol};
use crate::gate::{Gate, Verdict};
use crate::mcp::McpHub;
use crate::model::{ErrorKind, Finish, Message, Model, Request, Role, ToolCall, ToolChoice, ToolSpec};
use crate::perm::{self, Approver, Decision, DenyAll, Mode};
use crate::sandbox::{self, Sandbox};
use crate::skills::{self, Skill};
use crate::tools::{self, Ctx, Offer, Profile, Status as ToolStatus};
use crate::view::Views;
use crate::workspace::Workspace;
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Mutex, PoisonError};
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Config {
    pub mode: Mode,
    /// The model's context window in tokens.
    pub context_window: u32,
    /// Output tokens for one reply.
    pub max_output: u32,
    /// Model calls before the run stops.
    pub max_steps: u32,
    /// "Done" claims the tests may reject before the run stops.
    pub max_verify: u32,
    pub temperature: f32,
    pub command_timeout: Duration,
    /// Start the tests as soon as a round changed files, while the model writes its next reply.
    pub eager_tests: bool,
    /// Every reply must be a tool call; the work ends with the `done` tool.
    pub strict_tools: bool,
    /// Hand the model's text to the sink while it arrives.
    pub stream: bool,
    /// The tool set. `None` picks it from the context window.
    pub profile: Option<Profile>,
    /// An answer that changed nothing still has to pass the tests when the project has them.
    pub check_answers: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mode: Mode::Edit,
            context_window: 16_384,
            max_output: 2048,
            max_steps: 30,
            max_verify: 4,
            temperature: 0.2,
            command_timeout: Duration::from_secs(120),
            eager_tests: true,
            strict_tools: false,
            stream: true,
            profile: None,
            check_answers: true,
        }
    }
}

/// The same test failure this many times in a row ends the run.
const SAME_FAIL_LIMIT: u32 = 3;
/// Rounds in a row in which no call did anything useful.
const WASTED_LIMIT: u32 = 3;
/// Refusals in a row.
const DENIAL_LIMIT: u32 = 3;
const EMPTY_RETRIES: u32 = 2;
const CUT_RETRIES: u32 = 2;
const ANNOUNCE_NUDGES: u32 = 2;
/// Characters kept of a reply that hit the output limit.
const CUT_KEEP: usize = 800;
/// Files listed in the first message.
pub const FILES_IN_PROMPT: usize = 60;
/// Files looked at when the session starts, to match the names a task mentions.
const WALK_LIMIT: usize = 2000;
/// Files named in the task that are read before the first model call.
const PREFETCH_FILES: usize = 3;
const PREFETCH_MAX_BYTES: u64 = 200_000;
/// How long a round waits for the tests it started; a result that arrives in time goes into
/// the round's last tool result.
const EAGER_GRACE: Duration = Duration::from_millis(1500);
const MAX_NOTES: usize = 5;
const NOTE_CHARS: usize = 200;
const INSTRUCTION_FILES: &[&str] = &["AGENTS.md", "CAP.md"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Status {
    /// The model said it was done and the tests pass.
    Verified,
    /// The model said it was done and the project has no tests to check that with.
    Unverified,
    /// The model answered without changing anything, and there are no tests to run.
    Answered,
    /// The tests still fail after the allowed number of "done" claims.
    TestsFail,
    StepLimit,
    /// The model stopped making usable steps (broken calls, refused calls, empty replies).
    Stuck(String),
    ModelFailed(ErrorKind),
    /// The task and the newest steps do not fit in the context window.
    ContextFull,
    /// The person stopped the turn.
    Interrupted,
}

impl Status {
    pub fn as_str(&self) -> &'static str {
        match self {
            Status::Verified => "verified",
            Status::Unverified => "unverified",
            Status::Answered => "answered",
            Status::TestsFail => "tests_fail",
            Status::StepLimit => "step_limit",
            Status::Stuck(_) => "stuck",
            Status::ModelFailed(_) => "model_failed",
            Status::ContextFull => "context_full",
            Status::Interrupted => "interrupted",
        }
    }
}

/// The result of one turn.
#[derive(Clone, Debug)]
pub struct Report {
    pub status: Status,
    /// The model's last text, or what went wrong.
    pub text: String,
    pub steps: u32,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub seconds: f32,
    /// Files created or changed, as paths from the project root.
    pub changed: Vec<String>,
    /// "Done" claims the tests rejected.
    pub rejections: u32,
    /// The end of the last failing test output, when the run ended with failing tests.
    pub failure: Option<String>,
    pub transcript: Vec<Message>,
}

pub enum Event<'a> {
    Step(u32),
    /// A piece of the model's text while it arrives.
    Delta(&'a str),
    /// The complete text the model wrote next to its tool calls.
    Text(&'a str),
    /// A complete reply without tool calls.
    Answer(&'a str),
    Call {
        name: &'a str,
        args: &'a str,
    },
    Result {
        name: &'a str,
        status: ToolStatus,
        text: &'a str,
    },
    Gate(&'a Verdict),
    Nudge(&'a str),
    Compacted {
        stubbed: usize,
        dropped: usize,
    },
}

/// Where progress goes (terminal, JSON lines, nowhere).
pub trait Sink: Sync {
    fn event(&self, event: &Event<'_>);
}

pub struct Quiet;
impl Sink for Quiet {
    fn event(&self, _: &Event<'_>) {}
}

pub struct Agent<'a, M: Model> {
    pub model: &'a M,
    pub ws: &'a Workspace,
    pub gate: &'a Gate,
    pub approver: &'a dyn Approver,
    pub sink: &'a dyn Sink,
    pub config: Config,
    /// Set from outside (Ctrl-C) to stop the turn that is running.
    pub cancel: Option<&'a Cancel>,
    /// The isolation commands and tests run in.
    pub sandbox: &'a Sandbox,
    pub skills: &'a [Skill],
    pub mcp: Option<&'a McpHub>,
}

impl<'a, M: Model> Agent<'a, M> {
    /// An agent with the default settings: nothing is approved, nothing is printed, no isolation.
    pub fn new(model: &'a M, ws: &'a Workspace, gate: &'a Gate) -> Self {
        Agent {
            model,
            ws,
            gate,
            approver: &DenyAll,
            sink: &Quiet,
            config: Config::default(),
            cancel: None,
            sandbox: &sandbox::OFF,
            skills: &[],
            mcp: None,
        }
    }

    /// A new conversation.
    pub fn session(&'a self) -> Session<'a, M> {
        Session::new(self, Vec::new())
    }

    /// Continue an earlier conversation. Its system message is replaced by the current one.
    pub fn resume(&'a self, history: Vec<Message>) -> Session<'a, M> {
        Session::new(self, history)
    }

    /// One task from start to finish.
    pub async fn run(&'a self, task: &str) -> Report {
        self.session().turn(task).await
    }
}

pub fn system_prompt(offer: &Offer) -> String {
    let read_only = offer.mode == Mode::Read;
    let mut p = String::from(
        "You are CAP, a coding agent. You work in a project folder through tools.\n\n\
         How to work:\n\
         - Look before you change: read the file you are about to edit.\n",
    );
    if read_only {
        p.push_str("- This run is read-only: inspect the project and answer; files stay as they are.\n");
    } else {
        p.push_str("- Change files with replace_lines: give the line numbers that read_file shows and only the new lines.\n");
        if offer.profile != Profile::Small {
            p.push_str("- edit_file replaces an exact piece of text when line numbers do not fit the change.\n");
        }
    }
    if offer.has_tests {
        p.push_str("- After a change, call run_tests. A failing test tells you what to fix next.\n");
    }
    if offer.skills {
        p.push_str("- When a listed skill fits the task, call use_skill first and follow its instructions.\n");
    }
    if offer.profile == Profile::Full {
        p.push_str("- Keep a fact you will need later with note: one short line.\n");
    }
    if offer.done {
        p.push_str(
            "- When the task is finished, call done with one or two sentences on what you did.\n\n\
             Rules:\n\
             - Every reply is a tool call. Take the step instead of describing it.\n",
        );
    } else {
        p.push_str(
            "- When the task is finished, answer in plain text: one or two sentences on what you did. That answer has no tool call.\n\n\
             Rules:\n\
             - Until the work is finished, every reply is a tool call. Take the step instead of describing it.\n",
        );
    }
    if offer.has_tests {
        p.push_str(
            "- Saying the work is done starts the tests. If they fail, you get the output and continue.\n\
             - Fix the code, not the tests, unless the task is about the tests.\n",
        );
    }
    p.push_str("- Paths are relative to the project folder.\n");
    p.push_str("- Lines that start with [state] come from the harness and show where the work stands.\n");
    if offer.has_tests && !read_only {
        p.push_str("- A [tests after this change] line in a tool result is the harness's own test run for that change.\n");
    }
    p
}

/// Text that says what comes next instead of doing it ("Let me read the file.").
pub fn announces(text: &str) -> bool {
    let Some(last) = text.lines().rev().map(str::trim).find(|l| !l.is_empty()) else { return false };
    if text.chars().count() > 600 {
        return false;
    }
    let low = last.to_lowercase();
    let starts =
        ["let me ", "i'll ", "i will ", "i'm going to ", "i am going to ", "now i'll ", "now i will ", "now let me ", "next, ", "first, i"];
    let bare = last.trim_end_matches(['.', '!', ' ']);
    let ends = ["하겠습니다", "할게요", "볼게요", "보겠습니다", "드리겠습니다"];
    last.ends_with(':') || starts.iter().any(|s| low.starts_with(s)) || ends.iter().any(|e| bare.ends_with(e))
}

/// The project files a task mentions, in the order it mentions them. A word matches a file by
/// its path from the root, or by its file name when only one file has that name.
pub fn named_files(task: &str, files: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for raw in task.split(|c: char| c.is_whitespace() || "`\"'()[]<>,;*".contains(c)) {
        let ends = ['.', ':', '!', '?'];
        // A trailing particle ("calc.py를") is not part of the name.
        let word = raw.trim_end_matches(ends).trim_end_matches(|c: char| !c.is_ascii()).trim_end_matches(ends);
        let word = word.replace('\\', "/");
        let word = word.trim_start_matches("./");
        if word.is_empty() {
            continue;
        }
        let exact = files.iter().find(|f| f.as_str() == word);
        let hit = exact.or_else(|| {
            if !word.contains('.') || word.contains('/') {
                return None;
            }
            let mut same = files.iter().filter(|f| f.rsplit('/').next() == Some(word));
            match (same.next(), same.next()) {
                (Some(only), None) => Some(only),
                _ => None,
            }
        });
        if let Some(file) = hit
            && !out.contains(file)
        {
            out.push(file.clone());
            if out.len() == PREFETCH_FILES {
                break;
            }
        }
    }
    out
}

fn clip_chars(text: &str, max: usize) -> String {
    if text.chars().count() <= max { text.to_string() } else { text.chars().take(max).collect::<String>() + " [...]" }
}

/// The lines a `replace_lines` call left in the file, from its arguments.
fn line_range(args: &Map<String, Value>) -> Option<(usize, usize)> {
    let start = match args.get("start")? {
        Value::Number(n) => usize::try_from(n.as_u64()?).ok()?,
        Value::String(s) => s.trim().parse().ok()?,
        _ => return None,
    };
    let new = args.get("new").and_then(Value::as_str).unwrap_or("");
    let count = edit::lines_of(&Eol::to_lf(new)).len();
    (start > 0 && count > 0).then(|| (start, start + count - 1))
}

/// The project's own instructions for agents: the first of the known files that has text.
fn instructions(ws: &Workspace) -> Option<(&'static str, String)> {
    INSTRUCTION_FILES.iter().find_map(|name| {
        let path = ws.root().join(name);
        if std::fs::metadata(&path).map_or(true, |m| !m.is_file() || m.len() > PREFETCH_MAX_BYTES) {
            return None;
        }
        let text = std::fs::read_to_string(&path).ok()?;
        let text = text.trim();
        (!text.is_empty()).then(|| (*name, text.to_string()))
    })
}

/// What one handled tool call means for the loop's counters.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Worth {
    Useful,
    Wasted,
    Refused,
}

struct Handled {
    worth: Worth,
    status: ToolStatus,
    text: String,
    /// The call ended the turn (an accepted `done`).
    end: Option<(Status, String)>,
    /// The file whose lines the result shows.
    viewed: Option<String>,
}

impl Handled {
    fn new(worth: Worth, status: ToolStatus, text: impl Into<String>) -> Self {
        Handled { worth, status, text: text.into(), end: None, viewed: None }
    }

    fn wasted(text: impl Into<String>) -> Self {
        Handled::new(Worth::Wasted, ToolStatus::Failed, text)
    }
}

/// What the tests say about a "done" claim.
enum Claim {
    Final(Status),
    /// The tests fail; the text goes back to the model.
    Rejected(String),
}

type Pending<'a> = Pin<Box<dyn Future<Output = Verdict> + 'a>>;

/// The verdict of a test run that is under way; waits forever when there is none.
async fn maybe(pending: &mut Option<Pending<'_>>) -> Verdict {
    match pending {
        Some(run) => run.as_mut().await,
        None => std::future::pending().await,
    }
}

/// The counters of one turn.
struct Turn {
    started: Instant,
    steps: u32,
    prompt_tokens: u64,
    completion_tokens: u64,
    /// Files changed, with the lines of the last line edit when they are known.
    changed: BTreeMap<String, Option<(usize, usize)>>,
    /// A command that may have changed files has run.
    ran_command: bool,
    rejections: u32,
    last_failure: Option<(u64, u32)>,
    failure_tail: Option<String>,
    wasted: u32,
    denials: u32,
    empty: u32,
    cut: u32,
    announced: u32,
    /// A tool of the current round changed a file.
    round_changed: bool,
    /// The model's last text.
    last_text: String,
}

impl Turn {
    fn new() -> Self {
        Turn {
            started: Instant::now(),
            steps: 0,
            prompt_tokens: 0,
            completion_tokens: 0,
            changed: BTreeMap::new(),
            ran_command: false,
            rejections: 0,
            last_failure: None,
            failure_tail: None,
            wasted: 0,
            denials: 0,
            empty: 0,
            cut: 0,
            announced: 0,
            round_changed: false,
            last_text: String::new(),
        }
    }
}

/// One conversation with the model. It lives across turns, so a follow-up task continues with
/// what the model has already read and done.
pub struct Session<'a, M: Model> {
    agent: &'a Agent<'a, M>,
    /// The project's files when the session started, without secret files.
    files: Vec<String>,
    offer: Offer,
    specs: Vec<ToolSpec>,
    /// How many of `specs` are CAP's own tools; the rest come from outside servers.
    builtin: usize,
    tools_tokens: u32,
    msgs: Vec<Message>,
    cal: Calibration,
    /// The lines the model has in front of it.
    views: Mutex<Views>,
    /// For each file, the positions in `msgs` of the tool results that show its lines.
    view_at: HashMap<String, Vec<usize>>,
    /// Goes up when files change or old results leave the context; a read is "the same" only within one epoch.
    epoch: u64,
    reads: HashMap<String, u64>,
    failed_calls: HashMap<String, u32>,
    /// The last test result, valid while nothing has changed since.
    fresh_verdict: Option<Verdict>,
    /// How the last finished test run ended: passed, failed, or found no tests.
    last_run: Option<bool>,
    /// Something changed after the last test run.
    dirty: bool,
    /// A test run the harness started on its own.
    pending: Option<Pending<'a>>,
    notes: Vec<String>,
    /// Files in view, oldest first.
    read_files: Vec<String>,
    /// The first user message, with the project overview, has been written.
    seeded: bool,
    turn: Turn,
}

impl<'a, M: Model> Session<'a, M> {
    fn new(agent: &'a Agent<'a, M>, mut history: Vec<Message>) -> Self {
        let cfg = &agent.config;
        let has_tests = !matches!(agent.gate, Gate::None);
        let (walked, more) = agent.ws.files(agent.ws.root(), WALK_LIMIT);
        let files: Vec<String> = walked.into_iter().filter(|f| !perm::is_secret(f)).collect();
        let offer = Offer {
            mode: cfg.mode,
            has_tests,
            profile: cfg.profile.unwrap_or_else(|| Profile::for_window(cfg.context_window)),
            listing_cut: more || files.len() > FILES_IN_PROMPT,
            done: cfg.strict_tools,
            skills: !agent.skills.is_empty(),
        };
        let mut specs = tools::specs(&offer);
        let builtin = specs.len();
        if let Some(hub) = agent.mcp {
            // Outside tools are offered while their descriptions stay a small part of the window.
            // One that is left out still runs when the model calls it by name.
            let budget = cfg.context_window / 6;
            let mut used = 0;
            for spec in hub.specs() {
                let cost = context::estimate_tools(std::slice::from_ref(&spec));
                if used + cost > budget {
                    break;
                }
                used += cost;
                specs.push(spec);
            }
        }
        let system = Message::system(system_prompt(&offer));
        match history.first_mut() {
            Some(first) if first.role == Role::System => *first = system,
            _ => history.insert(0, system),
        }
        let seeded = history.iter().any(|m| m.role == Role::User);
        Session {
            agent,
            files,
            offer,
            tools_tokens: context::estimate_tools(&specs),
            specs,
            builtin,
            msgs: history,
            cal: Calibration::default(),
            views: Mutex::new(Views::default()),
            view_at: HashMap::new(),
            epoch: 0,
            reads: HashMap::new(),
            failed_calls: HashMap::new(),
            fresh_verdict: None,
            last_run: None,
            dirty: false,
            pending: None,
            notes: Vec::new(),
            read_files: Vec::new(),
            seeded,
            turn: Turn::new(),
        }
    }

    /// The conversation so far, starting with the system message.
    pub fn messages(&self) -> &[Message] {
        &self.msgs
    }

    pub fn into_history(self) -> Vec<Message> {
        self.msgs
    }

    /// The tools offered to the model.
    pub fn specs(&self) -> &[ToolSpec] {
        &self.specs
    }

    /// About how many tokens the next request takes before the model's reply.
    pub fn tokens(&self) -> u32 {
        self.cal.apply(context::estimate_messages(&self.msgs) + self.tools_tokens)
    }

    /// Give the model a task (or the next message of the conversation) and run until it is
    /// finished, stuck or stopped.
    pub async fn turn(&mut self, task: &str) -> Report {
        self.turn = Turn::new();
        self.epoch += 1;
        self.fresh_verdict = None;
        self.pending = None;
        self.failed_calls.clear();
        let message = if self.seeded { task.trim().to_string() } else { self.first_message(task) };
        self.seeded = true;
        self.msgs.push(Message::user(message));
        let (status, text) = self.go().await;
        // Tests the harness started on its own end with the turn.
        self.pending = None;
        Report {
            status,
            text,
            steps: self.turn.steps,
            prompt_tokens: self.turn.prompt_tokens,
            completion_tokens: self.turn.completion_tokens,
            seconds: self.turn.started.elapsed().as_secs_f32(),
            changed: self.turn.changed.keys().cloned().collect(),
            rejections: self.turn.rejections,
            failure: self.turn.failure_tail.clone(),
            transcript: self.msgs.clone(),
        }
    }

    fn emit(&self, event: Event<'_>) {
        self.agent.sink.event(&event);
    }

    fn nudge(&mut self, text: &str) {
        self.emit(Event::Nudge(text));
        self.msgs.push(Message::user(text));
    }

    fn views(&self) -> std::sync::MutexGuard<'_, Views> {
        self.views.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The first user message. What stays the same between tasks comes first, so a server that
    /// caches prompt prefixes can reuse it: instructions, files, skills, then the task's own parts.
    fn first_message(&mut self, task: &str) -> String {
        let agent = self.agent;
        // Everything next to the task shares a quarter of the window.
        let extras = agent.config.context_window / 4;
        let mut parts: Vec<String> = Vec::new();
        if let Some((name, text)) = instructions(agent.ws) {
            parts.push(format!("Project instructions ({name}):\n{}", clip_chars(&text, extras as usize)));
        }

        let shown: Vec<String> = self.files.iter().take(FILES_IN_PROMPT).cloned().collect();
        let lines = codemap::listing(agent.ws.root(), &shown, task, (extras / 3).min(1500));
        let mapped = lines.iter().zip(&shown).any(|(line, path)| line.len() > path.len());
        let mut listing = String::from(if mapped { "Project files (path: what is defined in it):\n" } else { "Project files:\n" });
        listing.push_str(&if lines.is_empty() { "(none yet)".to_string() } else { lines.join("\n") });
        if self.offer.listing_cut {
            listing.push_str("\n(more files exist; use list_files on a folder)");
        }
        parts.push(listing);

        if !agent.skills.is_empty() {
            parts.push(format!("Skills (call use_skill with a name to get its instructions):\n{}", skills::listing(agent.skills)));
        }

        let used: u32 = parts.iter().map(|p| context::estimate_text(p)).sum();
        let mut room = extras.saturating_sub(used) as usize * 3;
        let mut pages: Vec<String> = Vec::new();
        for rel in named_files(task, &self.files) {
            if room < 300 {
                break;
            }
            if let Some(page) = self.prefetch(&rel, room.min(tools::READ_CHARS)) {
                room = room.saturating_sub(page.len());
                pages.push(page);
            }
        }
        if !pages.is_empty() {
            parts.push(format!("Files named in the task below (already read for you):\n{}", pages.join("\n\n")));
        }

        parts.push(format!("Task:\n{}", task.trim()));
        parts.join("\n\n")
    }

    /// Read a file the task names the way `read_file` would, so the model starts with it in view.
    fn prefetch(&mut self, rel: &str, max_chars: usize) -> Option<String> {
        if perm::is_secret(rel) || perm::check_read(Some(rel)) != Decision::Allow {
            return None;
        }
        let abs = self.agent.ws.resolve(rel)?.abs;
        let meta = std::fs::metadata(&abs).ok()?;
        if !meta.is_file() || meta.len() == 0 || meta.len() > PREFETCH_MAX_BYTES {
            return None;
        }
        let bytes = std::fs::read(&abs).ok()?;
        if bytes.iter().take(8000).any(|&b| b == 0) {
            return None;
        }
        let text = Eol::to_lf(&String::from_utf8(bytes).ok()?);
        let total = edit::lines_of(&text).len();
        if total == 0 {
            return None;
        }
        let (page, last) = tools::page(rel, &text, 1, tools::READ_LINES, max_chars);
        self.views().saw(rel, &text, 1, last);
        self.read_files.push(rel.to_string());
        if last >= total {
            // The whole file is in view: reading it again adds nothing until something changes.
            self.reads.insert(format!("read_file {}", json!({ "path": rel })), self.epoch);
        }
        Some(page)
    }

    fn tests_state(&self) -> Option<Tests> {
        if !self.offer.has_tests {
            return None;
        }
        Some(if self.pending.is_some() {
            Tests::Running
        } else if self.dirty {
            Tests::Stale
        } else {
            match self.last_run {
                Some(true) => Tests::Pass,
                Some(false) => Tests::Fail,
                None => Tests::NotRun,
            }
        })
    }

    fn board(&self) -> String {
        let changed: Vec<(String, Option<(usize, usize)>)> = self.turn.changed.iter().map(|(path, range)| (path.clone(), *range)).collect();
        Board {
            step: self.turn.steps + 1,
            max_steps: self.agent.config.max_steps,
            changed: &changed,
            read: &self.read_files,
            tests: self.tests_state(),
            notes: &self.notes,
            done_tool: self.offer.done,
        }
        .render()
    }

    /// Old results left the context: what the model no longer sees does not count as seen.
    fn compacted(&mut self, stubbed: usize, dropped: usize) {
        self.epoch += 1;
        self.emit(Event::Compacted { stubbed, dropped });
        let mut views = self.views.lock().unwrap_or_else(PoisonError::into_inner);
        if dropped > 0 {
            // Whole rounds are gone and the positions moved.
            views.clear();
            self.view_at.clear();
            self.read_files.clear();
            return;
        }
        let msgs = &self.msgs;
        let mut gone: Vec<String> = Vec::new();
        self.view_at.retain(|path, at| {
            let lost = at.iter().any(|&i| msgs.get(i).is_none_or(|m| m.content.starts_with(context::STUB)));
            if lost {
                views.forget(path);
                gone.push(path.clone());
            }
            !lost
        });
        self.read_files.retain(|path| !gone.contains(path));
    }

    /// A file or the project changed: earlier reads, test results and failures are out of date.
    fn touched(&mut self) {
        self.epoch += 1;
        self.fresh_verdict = None;
        self.pending = None;
        self.failed_calls.clear();
        self.dirty = true;
    }

    /// Keep a finished test run for as long as nothing changes.
    fn store(&mut self, verdict: Verdict) {
        self.emit(Event::Gate(&verdict));
        self.last_run = match &verdict {
            Verdict::Pass => Some(true),
            Verdict::Fail { .. } => Some(false),
            Verdict::NoTests => None,
        };
        self.dirty = false;
        self.fresh_verdict = Some(verdict);
    }

    /// What the tests say now: the kept result, the run that is under way, or a new run.
    async fn verdict(&mut self) -> Verdict {
        if let Some(verdict) = self.fresh_verdict.clone() {
            return verdict;
        }
        let agent = self.agent;
        let verdict = match self.pending.take() {
            Some(mut run) => run.as_mut().await,
            None => agent.gate.run_in(agent.ws.root(), agent.sandbox).await,
        };
        self.store(verdict.clone());
        verdict
    }

    /// The model says the work is finished. The tests decide.
    async fn claim(&mut self) -> Claim {
        let cfg = &self.agent.config;
        let untouched = self.turn.changed.is_empty() && !self.turn.ran_command;
        if untouched && !cfg.check_answers {
            return Claim::Final(Status::Answered);
        }
        match self.verdict().await {
            Verdict::Pass => Claim::Final(Status::Verified),
            Verdict::NoTests if untouched => Claim::Final(Status::Answered),
            Verdict::NoTests => Claim::Final(Status::Unverified),
            Verdict::Fail { tail, signature } => {
                self.turn.rejections += 1;
                let same = match self.turn.last_failure {
                    Some((sig, n)) if sig == signature => n + 1,
                    _ => 1,
                };
                self.turn.last_failure = Some((signature, same));
                self.turn.failure_tail = Some(tail.clone());
                if self.turn.rejections >= cfg.max_verify || same >= SAME_FAIL_LIMIT {
                    return Claim::Final(Status::TestsFail);
                }
                // The model has now seen this result; a second claim without a change runs the tests again.
                self.fresh_verdict = None;
                Claim::Rejected(format!("The work is not finished: the tests fail.\n{tail}\nFind the cause, fix it and continue."))
            }
        }
    }

    async fn go(&mut self) -> (Status, String) {
        let agent = self.agent;
        let cfg = &agent.config;
        loop {
            if agent.cancel.is_some_and(Cancel::is_cancelled) {
                return (Status::Interrupted, String::new());
            }
            if self.turn.steps >= cfg.max_steps {
                return (Status::StepLimit, format!("stopped after {} steps without a finished result", self.turn.steps));
            }
            // The board goes into the request only; the stored history stays the same from call
            // to call, which keeps the prompt prefix stable.
            let has_board = self.turn.steps >= 1;
            let board_tokens = if has_board { context::estimate_text(&self.board()) + 8 } else { 0 };
            let fit = match context::fit(&mut self.msgs, self.tools_tokens + board_tokens, cfg.context_window, cfg.max_output, self.cal) {
                Ok(fit) => fit,
                Err(over) => {
                    return (Status::ContextFull, format!("about {} tokens are needed and the window holds {}", over.prompt, over.window));
                }
            };
            if fit.stubbed + fit.dropped > 0 {
                self.compacted(fit.stubbed, fit.dropped);
            }
            // Written after the fitting, so it lists only what is still in view.
            let board = has_board.then(|| self.board());
            self.turn.steps += 1;
            self.emit(Event::Step(self.turn.steps));
            let with_board: Option<Vec<Message>> = board.map(|board| {
                let mut copy = self.msgs.clone();
                match copy.last_mut() {
                    Some(last) if last.role == Role::User => {
                        last.content.push_str("\n\n");
                        last.content.push_str(&board);
                    }
                    _ => copy.push(Message::user(board)),
                }
                copy
            });

            // The model call. Tests the harness started keep running next to it, and Ctrl-C ends it.
            let mut pending = self.pending.take();
            let mut early: Option<Verdict> = None;
            let (outcome, estimated) = {
                let messages: &[Message] = with_board.as_deref().unwrap_or(&self.msgs);
                let estimated = context::estimate_messages(messages) + self.tools_tokens;
                let request = Request {
                    messages,
                    tools: &self.specs,
                    max_tokens: fit.max_tokens,
                    temperature: cfg.temperature,
                    tool_choice: if cfg.strict_tools { ToolChoice::Required } else { ToolChoice::Auto },
                };
                let sink = agent.sink;
                let on_text = move |piece: &str| sink.event(&Event::Delta(piece));
                let call =
                    async { if cfg.stream { agent.model.stream(request, &on_text).await } else { agent.model.complete(request).await } };
                tokio::pin!(call);
                let outcome = loop {
                    tokio::select! {
                        biased;
                        _ = cancel::cancelled(agent.cancel) => break None,
                        verdict = maybe(&mut pending), if pending.is_some() => {
                            early = Some(verdict);
                            pending = None;
                        }
                        reply = &mut call => break Some(reply),
                    }
                };
                (outcome, estimated)
            };
            self.pending = pending;
            if let Some(verdict) = early {
                self.store(verdict);
            }
            let reply = match outcome {
                None => {
                    self.msgs.push(Message::assistant("(interrupted)", Vec::new()));
                    return (Status::Interrupted, String::new());
                }
                Some(Err(e)) => return (Status::ModelFailed(e.kind), e.to_string()),
                Some(Ok(reply)) => reply,
            };
            self.turn.prompt_tokens += u64::from(reply.usage.prompt_tokens);
            self.turn.completion_tokens += u64::from(reply.usage.completion_tokens);
            self.cal.learn(estimated, reply.usage.prompt_tokens);

            let text = reply.content.trim().to_string();
            if !reply.tool_calls.is_empty() {
                // Text next to a call is commentary; text alone is the answer and goes out in the report.
                if !text.is_empty() {
                    self.emit(Event::Text(&text));
                    self.turn.last_text = text.clone();
                }
                self.msgs.push(Message::assistant(clip_chars(&text, CUT_KEEP), reply.tool_calls.clone()));
                if let Some(stop) = self.round(&reply.tool_calls, reply.finish == Finish::Length).await {
                    return stop;
                }
                continue;
            }

            if reply.finish == Finish::Length {
                self.turn.cut += 1;
                if self.turn.cut > CUT_RETRIES {
                    return (Status::Stuck("replies keep hitting the output limit".into()), self.turn.last_text.clone());
                }
                self.msgs.push(Message::assistant(clip_chars(&text, CUT_KEEP), Vec::new()));
                self.nudge("Your reply was cut off at the output limit. Write less: make the next change as one tool call that holds only the lines that change.");
                continue;
            }
            if text.is_empty() {
                self.turn.empty += 1;
                if self.turn.empty > EMPTY_RETRIES {
                    return (Status::Stuck("the model keeps sending empty replies".into()), self.turn.last_text.clone());
                }
                self.msgs.push(Message::assistant("(empty reply)", Vec::new()));
                self.nudge("Your reply was empty. Call a tool to continue, or say in one sentence that the work is finished.");
                continue;
            }
            self.emit(Event::Answer(&text));
            self.msgs.push(Message::assistant(text.clone(), Vec::new()));
            if announces(&text) && self.turn.announced < ANNOUNCE_NUDGES {
                self.turn.announced += 1;
                self.nudge("You said what you will do and did not do it. Call the tool now.");
                continue;
            }
            self.turn.last_text = text.clone();

            let claim = tokio::select! {
                biased;
                _ = cancel::cancelled(agent.cancel) => None,
                claim = self.claim() => Some(claim),
            };
            match claim {
                None => return (Status::Interrupted, String::new()),
                Some(Claim::Final(status)) => return (status, text),
                Some(Claim::Rejected(told)) => self.nudge(&told),
            }
        }
    }

    /// Run the tool calls of one reply. Returns a final status when the turn should stop.
    async fn round(&mut self, calls: &[ToolCall], was_cut: bool) -> Option<(Status, String)> {
        let agent = self.agent;
        self.turn.round_changed = false;
        let mut any_useful = false;
        let mut end: Option<(Status, String)> = None;
        for (i, call) in calls.iter().enumerate() {
            if end.is_some() {
                self.msgs.push(Message::tool(call.id.clone(), "[skipped: the work was already declared finished]"));
                continue;
            }
            self.emit(Event::Call { name: &call.name, args: &call.args });
            let handled = tokio::select! {
                biased;
                _ = cancel::cancelled(agent.cancel) => None,
                handled = self.handle(call, was_cut) => Some(handled),
            };
            let Some(handled) = handled else {
                // Every call of the reply needs a result for the history to stay well formed.
                for rest in &calls[i..] {
                    self.msgs.push(Message::tool(rest.id.clone(), "[interrupted]"));
                }
                // The stopped step may have changed something before it ended.
                self.touched();
                return Some((Status::Interrupted, String::new()));
            };
            self.emit(Event::Result { name: &call.name, status: handled.status, text: &handled.text });
            if let Some(path) = handled.viewed {
                self.view_at.entry(path).or_default().push(self.msgs.len());
            }
            self.msgs.push(Message::tool(call.id.clone(), handled.text));
            match handled.worth {
                Worth::Useful => {
                    any_useful = true;
                    self.turn.denials = 0;
                }
                Worth::Refused => self.turn.denials += 1,
                Worth::Wasted => {}
            }
            end = handled.end;
        }
        if end.is_some() {
            return end;
        }
        if self.turn.denials >= DENIAL_LIMIT {
            return Some((Status::Stuck("the model keeps asking for steps that are refused".into()), String::new()));
        }
        self.turn.wasted = if any_useful { 0 } else { self.turn.wasted + 1 };
        if self.turn.wasted >= WASTED_LIMIT {
            return Some((Status::Stuck("the model keeps sending tool calls that cannot run".into()), String::new()));
        }

        // The harness works ahead: after a change it starts the tests on its own. A fast suite
        // answers within the grace time and the result rides along with the change; a slow one
        // keeps running while the model writes its next reply.
        if self.turn.round_changed && agent.config.eager_tests && self.offer.has_tests && self.dirty && self.fresh_verdict.is_none() {
            let mut run: Pending<'a> = Box::pin(agent.gate.run_in(agent.ws.root(), agent.sandbox));
            let got = tokio::select! {
                biased;
                _ = cancel::cancelled(agent.cancel) => None,
                verdict = run.as_mut() => Some(verdict),
                _ = tokio::time::sleep(EAGER_GRACE) => None,
            };
            match got {
                Some(verdict) => {
                    let line = match &verdict {
                        Verdict::Pass => Some("\n[tests after this change: all pass]".to_string()),
                        Verdict::Fail { tail, .. } => Some(format!("\n[tests after this change: fail]\n{tail}")),
                        Verdict::NoTests => None,
                    };
                    self.store(verdict);
                    if let (Some(line), Some(last)) = (line, self.msgs.last_mut()) {
                        last.content.push_str(&line);
                    }
                }
                None => self.pending = Some(run),
            }
        }
        None
    }

    /// Whether a tool of CAP's own can be called in this session.
    fn exists(&self, name: &str) -> bool {
        match name {
            "replace_lines" | "edit_file" | "write_file" => self.offer.mode != Mode::Read,
            "run_tests" => self.offer.has_tests,
            "use_skill" => self.offer.skills,
            _ => true,
        }
    }

    fn tool_names(&self) -> String {
        let mut names = self.specs[..self.builtin].iter().map(|s| s.name.as_ref()).collect::<Vec<_>>().join(", ");
        let outside = self.agent.mcp.map_or(0, |hub| hub.tools().len());
        if outside > 0 {
            names.push_str(&format!(", and {outside} more from outside servers (their names start with mcp__)"));
        }
        names
    }

    async fn handle(&mut self, call: &ToolCall, was_cut: bool) -> Handled {
        let agent = self.agent;
        let cfg = &agent.config;
        let outside = agent.mcp.filter(|hub| hub.allowed(&call.name).is_some());
        let name: &str = if outside.is_some() {
            &call.name
        } else {
            match tools::canonical_name(&call.name).filter(|n| self.exists(n)) {
                Some(name) => name,
                None => return Handled::wasted(format!("There is no tool named {}. The tools are: {}.", call.name, self.tool_names())),
            }
        };
        let raw = if call.args.trim().is_empty() { "{}" } else { call.args.as_str() };
        let mut args: Map<String, Value> = match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(map)) => map,
            Ok(_) => return Handled::wasted(format!("The arguments of {name} must be one JSON object.")),
            Err(e) => {
                let hint = if was_cut { " Your reply was cut off at the output limit: send fewer lines in one call." } else { "" };
                return Handled::wasted(format!("The arguments of {name} are not valid JSON ({e}).{hint}"));
            }
        };
        if outside.is_none() {
            tools::normalize_args(name, &mut args);
        }
        let key = format!("{name} {}", Value::Object(args.clone()));
        if self.failed_calls.get(&key).is_some_and(|n| *n >= 2) {
            return Handled::wasted("This exact call has failed twice. Change the arguments or take a different step.");
        }
        if name == "read_file" && self.reads.get(&key) == Some(&self.epoch) {
            return Handled::wasted(
                "You already have this part of the file above and nothing has changed since. Take the next step with what you know.",
            );
        }

        if let Some(hub) = outside {
            let why = "it calls a tool of an outside server (MCP)";
            let free = cfg.mode == Mode::Auto || hub.allowed(name) == Some(true);
            if !free && !agent.approver.approve(&clip_chars(&key, 300), why) {
                let text = format!("Not approved: {why}. Go on without this step, or finish and say what is left.");
                return Handled::new(Worth::Refused, ToolStatus::Denied, text);
            }
            return match hub.call(name, &args).await {
                Ok(text) => {
                    // An outside tool may have changed anything.
                    self.touched();
                    self.turn.ran_command = true;
                    Handled::new(Worth::Useful, ToolStatus::Ok, clip_chars(&text, tools::READ_CHARS))
                }
                Err(e) => {
                    *self.failed_calls.entry(key).or_insert(0) += 1;
                    Handled::wasted(e)
                }
            };
        }

        match name {
            "run_tests" => {
                let text = match self.verdict().await {
                    Verdict::Pass => "run_tests: all tests pass".to_string(),
                    Verdict::NoTests => "run_tests: no tests were found".to_string(),
                    Verdict::Fail { tail, .. } => format!("run_tests: tests fail\n{tail}"),
                };
                return Handled::new(Worth::Useful, ToolStatus::Ok, text);
            }
            "done" => {
                let summary = args.get("summary").and_then(Value::as_str).unwrap_or("").trim().to_string();
                return match self.claim().await {
                    Claim::Final(status) => {
                        let text = if summary.is_empty() { self.turn.last_text.clone() } else { summary };
                        Handled { end: Some((status, text)), ..Handled::new(Worth::Useful, ToolStatus::Ok, "done") }
                    }
                    Claim::Rejected(told) => Handled::new(Worth::Useful, ToolStatus::Failed, told),
                };
            }
            "note" => {
                let text = args.get("text").and_then(Value::as_str).unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
                if text.is_empty() {
                    return Handled::wasted("note needs a text: one short fact worth keeping.");
                }
                if self.notes.len() == MAX_NOTES {
                    self.notes.remove(0);
                }
                self.notes.push(clip_chars(&text, NOTE_CHARS));
                return Handled::new(Worth::Useful, ToolStatus::Ok, "noted");
            }
            "use_skill" => {
                let wanted = args.get("name").and_then(Value::as_str).unwrap_or("");
                let Some(skill) = skills::find(agent.skills, wanted) else {
                    *self.failed_calls.entry(key).or_insert(0) += 1;
                    let names = agent.skills.iter().map(|s| s.name.as_str()).collect::<Vec<_>>().join(", ");
                    return Handled::wasted(format!("There is no skill named {wanted}. The skills are: {names}."));
                };
                let mut text = format!("Skill {}:\n{}", skill.name, skill.body);
                if let Ok(dir) = skill.dir.strip_prefix(agent.ws.root()) {
                    text = clip_chars(&text, tools::READ_CHARS);
                    text.push_str(&format!("\n[the skill's own files are in {}]", dir.to_string_lossy().replace('\\', "/")));
                }
                return Handled::new(Worth::Useful, ToolStatus::Ok, clip_chars(&text, tools::READ_CHARS + 200));
            }
            _ => {}
        }

        // A command that does more than look may change anything, the tests' files included.
        let acts = name == "run"
            && args.get("command").and_then(Value::as_str).is_none_or(|c| perm::check_command(Mode::Read, c) != Decision::Allow);
        if acts {
            self.pending = None;
        }
        let ctx = Ctx {
            ws: agent.ws,
            mode: cfg.mode,
            approver: agent.approver,
            command_timeout: cfg.command_timeout,
            views: &self.views,
            sandbox: agent.sandbox,
        };
        let result = tools::run_tool(&ctx, name, &args).await;
        let mut viewed = None;
        let file_changed = result.changed.is_some();
        if let Some(rel) = result.changed {
            let range = if name == "replace_lines" { line_range(&args) } else { None };
            self.turn.changed.insert(rel.clone(), range);
            self.turn.round_changed = true;
            // The result of an edit shows the lines around it.
            viewed = Some(rel);
        }
        let acted = acts && result.status != ToolStatus::Denied;
        self.turn.ran_command |= acted;
        if file_changed || acted {
            self.touched();
        }
        let (worth, status) = match result.status {
            ToolStatus::Ok => {
                if name == "read_file" {
                    self.reads.insert(key, self.epoch);
                    let rel = args.get("path").and_then(Value::as_str).and_then(|p| agent.ws.resolve(p)).map(|r| r.rel);
                    if let Some(rel) = rel {
                        self.read_files.retain(|f| f != &rel);
                        self.read_files.push(rel.clone());
                        viewed = Some(rel);
                    }
                }
                (Worth::Useful, ToolStatus::Ok)
            }
            ToolStatus::Failed => {
                *self.failed_calls.entry(key).or_insert(0) += 1;
                // A failed command still ran and told the model something; a failed edit or read did not move the work.
                (if name == "run" { Worth::Useful } else { Worth::Wasted }, ToolStatus::Failed)
            }
            ToolStatus::Denied => (Worth::Refused, ToolStatus::Denied),
        };
        Handled { viewed, ..Handled::new(worth, status, result.text) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn announcements_are_told_from_answers() {
        for t in [
            "Let me read the file first.",
            "I'll fix the bug now.",
            "먼저 파일을 읽어 보겠습니다.",
            "Here is the fix:",
            "테스트를 실행할게요",
        ] {
            assert!(announces(t), "{t}");
        }
        for t in ["Fixed add() to return a + b; the tests pass.", "add 함수의 부호를 고쳤습니다.", "", "Done."] {
            assert!(!announces(t), "{t}");
        }
    }

    #[test]
    fn the_prompt_names_only_tools_that_exist() {
        let read = system_prompt(&Offer::new(Mode::Read, false, Profile::Standard));
        assert!(!read.contains("replace_lines") && !read.contains("edit_file") && !read.contains("run_tests"));
        let small = system_prompt(&Offer::new(Mode::Edit, true, Profile::Small));
        assert!(small.contains("replace_lines") && small.contains("run_tests") && !small.contains("edit_file") && !small.contains("note"));
        let full = system_prompt(&Offer { skills: true, done: true, ..Offer::new(Mode::Edit, true, Profile::Full) });
        assert!(full.contains("edit_file") && full.contains("use_skill") && full.contains("note") && full.contains("call done"));
        assert!(!full.contains("answer in plain text"));
    }

    #[test]
    fn a_task_names_files_by_path_or_by_a_unique_name() {
        let files: Vec<String> = ["calc.py", "src/app/main.rs", "src/util.rs", "tests/test_calc.py", "a/mod.rs", "b/mod.rs", "Makefile"]
            .map(String::from)
            .to_vec();
        assert_eq!(named_files("Fix add() in `calc.py`, then look at util.rs.", &files), ["calc.py", "src/util.rs"]);
        // Korean particles, Windows separators and a leading ./ are not part of the name.
        assert_eq!(named_files("calc.py를 고치고 .\\src\\app\\main.rs도 봐줘", &files), ["calc.py", "src/app/main.rs"]);
        // A name two files share says nothing; a path does.
        assert_eq!(named_files("mod.rs is wrong, see b/mod.rs (and the Makefile).", &files), ["b/mod.rs", "Makefile"]);
        assert!(named_files("add() returns the wrong result. Fix it.", &files).is_empty());
        let many = named_files("calc.py src/util.rs Makefile a/mod.rs calc.py", &files);
        assert_eq!(many, ["calc.py", "src/util.rs", "Makefile"]);
    }

    #[test]
    fn a_line_edit_reports_the_lines_it_left() {
        let args = |v: Value| v.as_object().unwrap().clone();
        assert_eq!(line_range(&args(json!({"path": "a", "start": 3, "end": 9, "new": "x\ny\n"}))), Some((3, 4)));
        assert_eq!(line_range(&args(json!({"path": "a", "start": "7", "end": 7, "new": "x"}))), Some((7, 7)));
        assert_eq!(line_range(&args(json!({"path": "a", "start": 2, "end": 3, "new": ""}))), None);
        assert_eq!(line_range(&args(json!({"path": "a", "new": "x"}))), None);
    }
}
