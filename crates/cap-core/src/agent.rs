//! The agent loop. The model proposes a step; the code here runs it, judges it and decides
//! when the work counts as finished.

use crate::context::{self, Calibration};
use crate::gate::{Gate, Verdict};
use crate::model::{ErrorKind, Finish, Message, Model, Request, ToolCall, ToolSpec};
use crate::perm::{self, Approver, Decision, Mode};
use crate::tools::{self, Ctx, Status as ToolStatus};
use crate::workspace::Workspace;
use serde_json::{Map, Value};
use std::collections::{BTreeSet, HashMap};
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
const FILES_IN_PROMPT: usize = 60;

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
        }
    }
}

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
    Text(&'a str),
    Call { name: &'a str, args: &'a str },
    Result { name: &'a str, status: ToolStatus, text: &'a str },
    Gate(&'a Verdict),
    Nudge(&'a str),
    Compacted { stubbed: usize, dropped: usize },
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
}

pub fn system_prompt(mode: Mode, has_tests: bool) -> String {
    let mut p = String::from(
        "You are CAP, a coding agent. You work in a project folder through tools.\n\n\
         How to work:\n\
         - Look before you change: read the file you are about to edit.\n",
    );
    if mode == Mode::Read {
        p.push_str("- This run is read-only: inspect the project and answer; files stay as they are.\n");
    } else {
        p.push_str("- Change files with edit_file, giving only the few lines that change.\n");
    }
    if has_tests {
        p.push_str("- After a change, call run_tests. A failing test tells you what to fix next.\n");
    }
    p.push_str(
        "- When the task is finished, answer in plain text: one or two sentences on what you did. That answer has no tool call.\n\n\
         Rules:\n\
         - Until the work is finished, every reply is a tool call. Take the step instead of describing it.\n",
    );
    if has_tests {
        p.push_str(
            "- Saying the work is done starts the tests. If they fail, you get the output and continue.\n\
             - Fix the code, not the tests, unless the task is about the tests.\n",
        );
    }
    p.push_str("- Paths are relative to the project folder.\n");
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

fn clip_chars(text: &str, max: usize) -> String {
    if text.chars().count() <= max { text.to_string() } else { text.chars().take(max).collect::<String>() + " [...]" }
}

/// What one handled tool call means for the loop's counters.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Worth {
    Useful,
    Wasted,
    Refused,
}

struct Run<'a, M: Model> {
    agent: &'a Agent<'a, M>,
    specs: Vec<ToolSpec>,
    msgs: Vec<Message>,
    started: Instant,
    steps: u32,
    prompt_tokens: u64,
    completion_tokens: u64,
    cal: Calibration,
    changed: BTreeSet<String>,
    /// A command that may have changed files has run.
    ran_command: bool,
    /// Goes up when files change or old results leave the context; a read is "the same" only within one epoch.
    epoch: u64,
    reads: HashMap<String, u64>,
    failed_calls: HashMap<String, u32>,
    /// The last test result, valid while nothing has changed since.
    fresh_verdict: Option<Verdict>,
    rejections: u32,
    last_failure: Option<(u64, u32)>,
    failure_tail: Option<String>,
    wasted: u32,
    denials: u32,
    empty: u32,
    cut: u32,
    announced: u32,
}

impl<'a, M: Model> Agent<'a, M> {
    pub async fn run(&'a self, task: &str) -> Report {
        let has_tests = !matches!(self.gate, Gate::None);
        let (files, more) = self.ws.files(self.ws.root(), FILES_IN_PROMPT);
        let files: Vec<String> = files.into_iter().filter(|f| !perm::is_secret(f)).collect();
        let mut first =
            format!("Task:\n{}\n\nProject files:\n{}", task.trim(), if files.is_empty() { "(none yet)".into() } else { files.join("\n") });
        if more {
            first.push_str("\n(more files exist; use list_files on a folder)");
        }
        let mut run = Run {
            agent: self,
            specs: tools::specs(self.config.mode, has_tests),
            msgs: vec![Message::system(system_prompt(self.config.mode, has_tests)), Message::user(first)],
            started: Instant::now(),
            steps: 0,
            prompt_tokens: 0,
            completion_tokens: 0,
            cal: Calibration::default(),
            changed: BTreeSet::new(),
            ran_command: false,
            epoch: 0,
            reads: HashMap::new(),
            failed_calls: HashMap::new(),
            fresh_verdict: None,
            rejections: 0,
            last_failure: None,
            failure_tail: None,
            wasted: 0,
            denials: 0,
            empty: 0,
            cut: 0,
            announced: 0,
        };
        let (status, text) = run.go().await;
        Report {
            status,
            text,
            steps: run.steps,
            prompt_tokens: run.prompt_tokens,
            completion_tokens: run.completion_tokens,
            seconds: run.started.elapsed().as_secs_f32(),
            changed: run.changed.into_iter().collect(),
            rejections: run.rejections,
            failure: run.failure_tail,
            transcript: run.msgs,
        }
    }
}

impl<M: Model> Run<'_, M> {
    fn emit(&self, event: Event<'_>) {
        self.agent.sink.event(&event);
    }

    fn nudge(&mut self, text: &str) {
        self.emit(Event::Nudge(text));
        self.msgs.push(Message::user(text));
    }

    async fn go(&mut self) -> (Status, String) {
        let cfg = &self.agent.config;
        let tools_tokens = context::estimate_tools(&self.specs);
        let mut last_text = String::new();
        loop {
            if self.steps >= cfg.max_steps {
                return (Status::StepLimit, format!("stopped after {} steps without a finished result", self.steps));
            }
            let fit = match context::fit(&mut self.msgs, tools_tokens, cfg.context_window, cfg.max_output, self.cal) {
                Ok(fit) => fit,
                Err(over) => {
                    return (Status::ContextFull, format!("about {} tokens are needed and the window holds {}", over.prompt, over.window));
                }
            };
            if fit.stubbed + fit.dropped > 0 {
                self.epoch += 1;
                self.emit(Event::Compacted { stubbed: fit.stubbed, dropped: fit.dropped });
            }
            self.steps += 1;
            self.emit(Event::Step(self.steps));
            let estimated = context::estimate_messages(&self.msgs) + tools_tokens;
            let request = Request { messages: &self.msgs, tools: &self.specs, max_tokens: fit.max_tokens, temperature: cfg.temperature };
            let reply = match self.agent.model.complete(request).await {
                Ok(reply) => reply,
                Err(e) => return (Status::ModelFailed(e.kind), e.to_string()),
            };
            self.prompt_tokens += u64::from(reply.usage.prompt_tokens);
            self.completion_tokens += u64::from(reply.usage.completion_tokens);
            self.cal.learn(estimated, reply.usage.prompt_tokens);

            let text = reply.content.trim().to_string();
            if !reply.tool_calls.is_empty() {
                // Text next to a call is commentary; text alone is the answer and goes out in the report.
                if !text.is_empty() {
                    self.emit(Event::Text(&text));
                }
                self.msgs.push(Message::assistant(clip_chars(&text, CUT_KEEP), reply.tool_calls.clone()));
                if let Some(stop) = self.round(&reply.tool_calls, reply.finish == Finish::Length).await {
                    return stop;
                }
                continue;
            }

            if reply.finish == Finish::Length {
                self.cut += 1;
                if self.cut > CUT_RETRIES {
                    return (Status::Stuck("replies keep hitting the output limit".into()), last_text);
                }
                self.msgs.push(Message::assistant(clip_chars(&text, CUT_KEEP), Vec::new()));
                self.nudge("Your reply was cut off at the output limit. Write less: make the next change as one tool call that holds only the lines that change.");
                continue;
            }
            if text.is_empty() {
                self.empty += 1;
                if self.empty > EMPTY_RETRIES {
                    return (Status::Stuck("the model keeps sending empty replies".into()), last_text);
                }
                self.msgs.push(Message::assistant("(empty reply)", Vec::new()));
                self.nudge("Your reply was empty. Call a tool to continue, or say in one sentence that the work is finished.");
                continue;
            }
            self.msgs.push(Message::assistant(text.clone(), Vec::new()));
            if announces(&text) && self.announced < ANNOUNCE_NUDGES {
                self.announced += 1;
                self.nudge("You said what you will do and did not do it. Call the tool now.");
                continue;
            }
            last_text = text;

            // A "done" claim. The tests decide.
            let verdict = match self.fresh_verdict.clone() {
                Some(v) => v,
                None => self.check().await,
            };
            match verdict {
                Verdict::Pass => return (Status::Verified, last_text),
                Verdict::NoTests if self.changed.is_empty() && !self.ran_command => return (Status::Answered, last_text),
                Verdict::NoTests => return (Status::Unverified, last_text),
                Verdict::Fail { tail, signature } => {
                    self.rejections += 1;
                    let same = match self.last_failure {
                        Some((sig, n)) if sig == signature => n + 1,
                        _ => 1,
                    };
                    self.last_failure = Some((signature, same));
                    self.failure_tail = Some(tail.clone());
                    if self.rejections >= cfg.max_verify || same >= SAME_FAIL_LIMIT {
                        return (Status::TestsFail, last_text);
                    }
                    // The model has now seen this result; a second claim without a change runs the tests again.
                    self.fresh_verdict = None;
                    self.nudge(&format!("The work is not finished: the tests fail.\n{tail}\nFind the cause, fix it and continue."));
                }
            }
        }
    }

    async fn check(&mut self) -> Verdict {
        let verdict = self.agent.gate.run(self.agent.ws.root()).await;
        self.emit(Event::Gate(&verdict));
        self.fresh_verdict = Some(verdict.clone());
        verdict
    }

    /// Run the tool calls of one reply. Returns a final status when the run should stop.
    async fn round(&mut self, calls: &[ToolCall], was_cut: bool) -> Option<(Status, String)> {
        let mut any_useful = false;
        for call in calls {
            self.emit(Event::Call { name: &call.name, args: &call.args });
            let (worth, status, text) = self.handle(call, was_cut).await;
            self.emit(Event::Result { name: &call.name, status, text: &text });
            self.msgs.push(Message::tool(call.id.clone(), text));
            match worth {
                Worth::Useful => {
                    any_useful = true;
                    self.denials = 0;
                }
                Worth::Refused => self.denials += 1,
                Worth::Wasted => {}
            }
        }
        if self.denials >= DENIAL_LIMIT {
            return Some((Status::Stuck("the model keeps asking for steps that are refused".into()), String::new()));
        }
        self.wasted = if any_useful { 0 } else { self.wasted + 1 };
        if self.wasted >= WASTED_LIMIT {
            return Some((Status::Stuck("the model keeps sending tool calls that cannot run".into()), String::new()));
        }
        None
    }

    async fn handle(&mut self, call: &ToolCall, was_cut: bool) -> (Worth, ToolStatus, String) {
        let wasted = |text: String| (Worth::Wasted, ToolStatus::Failed, text);
        let offered: Vec<&str> = self.specs.iter().map(|s| s.name).collect();
        let Some(name) = tools::canonical_name(&call.name).filter(|n| offered.contains(n)) else {
            return wasted(format!("There is no tool named {}. The tools are: {}.", call.name, offered.join(", ")));
        };
        let raw = if call.args.trim().is_empty() { "{}" } else { call.args.as_str() };
        let mut args: Map<String, Value> = match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(map)) => map,
            Ok(_) => return wasted(format!("The arguments of {name} must be one JSON object.")),
            Err(e) => {
                let hint = if was_cut { " Your reply was cut off at the output limit: send fewer lines in one call." } else { "" };
                return wasted(format!("The arguments of {name} are not valid JSON ({e}).{hint}"));
            }
        };
        tools::normalize_args(name, &mut args);
        let key = format!("{name} {}", Value::Object(args.clone()));
        if self.failed_calls.get(&key).is_some_and(|n| *n >= 2) {
            return wasted("This exact call has failed twice. Change the arguments or take a different step.".into());
        }
        if name == "read_file" && self.reads.get(&key) == Some(&self.epoch) {
            return wasted(
                "You already have this part of the file above and nothing has changed since. Take the next step with what you know.".into(),
            );
        }

        if name == "run_tests" {
            let text = match self.check().await {
                Verdict::Pass => "run_tests: all tests pass".to_string(),
                Verdict::NoTests => "run_tests: no tests were found".to_string(),
                Verdict::Fail { tail, .. } => format!("run_tests: tests fail\n{tail}"),
            };
            return (Worth::Useful, ToolStatus::Ok, text);
        }

        let cfg = &self.agent.config;
        let ctx = Ctx { ws: self.agent.ws, mode: cfg.mode, approver: self.agent.approver, command_timeout: cfg.command_timeout };
        let result = tools::run_tool(&ctx, name, &args).await;
        let file_changed = result.changed.is_some();
        if let Some(rel) = result.changed {
            self.changed.insert(rel);
        }
        // A command that does more than look may have changed anything.
        let acted = name == "run"
            && result.status != ToolStatus::Denied
            && args.get("command").and_then(Value::as_str).is_none_or(|c| perm::check_command(Mode::Read, c) != Decision::Allow);
        self.ran_command |= acted;
        if file_changed || acted {
            self.epoch += 1;
            self.fresh_verdict = None;
            self.failed_calls.clear();
        }
        match result.status {
            ToolStatus::Ok => {
                if name == "read_file" {
                    self.reads.insert(key, self.epoch);
                }
                (Worth::Useful, ToolStatus::Ok, result.text)
            }
            ToolStatus::Failed => {
                *self.failed_calls.entry(key).or_insert(0) += 1;
                // A failed command still ran and told the model something; a failed edit or read did not move the work.
                let worth = if name == "run" { Worth::Useful } else { Worth::Wasted };
                (worth, ToolStatus::Failed, result.text)
            }
            ToolStatus::Denied => (Worth::Refused, ToolStatus::Denied, result.text),
        }
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
        let read = system_prompt(Mode::Read, false);
        assert!(!read.contains("edit_file") && !read.contains("run_tests"));
        let edit = system_prompt(Mode::Edit, true);
        assert!(edit.contains("edit_file") && edit.contains("run_tests"));
    }
}
