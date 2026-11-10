//! Conversations over several turns, interrupts, the tests the harness starts on its own, and
//! what each request to the model holds. Driven by a scripted model in a temporary project.

use cap_core::agent::{Agent, Config, Event, Report, Sink, Status};
use cap_core::cancel::Cancel;
use cap_core::context::STUB;
use cap_core::gate::{self, Gate, Verdict};
use cap_core::mcp::{Client, McpHub, PROTOCOL_VERSION};
use cap_core::model::{Message, Model, ModelError, Reply, Request, Role, ToolChoice};
use cap_core::perm::{AllowAll, DenyAll};
use cap_core::skills;
use cap_core::testing::{Recorded, ScriptedModel, TempDir, call, calls, say};
use cap_core::tools::Profile;
use cap_core::workspace::Workspace;
use serde_json::{Value, json};
use std::path::Path;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream};

const BROKEN: &str = "def add(a, b):\n    return a - b\n";
const FIX: &str = r#"{"path": "calc.py", "old": "    return a - b", "new": "    return a + b"}"#;
const READ: &str = r#"{"path": "calc.py"}"#;
const TASK: &str = "add() returns the wrong result. Fix it.";

fn project() -> TempDir {
    let t = TempDir::new("session");
    t.write("calc.py", BROKEN).write("tests/test_calc.py", "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n");
    t
}

/// Passes once `calc.py` adds, and counts how often it was asked.
fn counted(runs: Arc<AtomicU32>) -> Gate {
    Gate::Custom(Box::new(move |root: &Path| {
        runs.fetch_add(1, Ordering::Relaxed);
        let code = std::fs::read_to_string(root.join("calc.py")).unwrap_or_default();
        if code.contains("a + b") {
            Verdict::Pass
        } else {
            let out = "FAILED tests/test_calc.py::test_add - assert -1 == 3";
            Verdict::Fail { tail: out.into(), signature: gate::signature(out) }
        }
    }))
}

fn adds() -> Gate {
    counted(Arc::new(AtomicU32::new(0)))
}

async fn run_with(t: &TempDir, model: &ScriptedModel, gate: &Gate, config: Config) -> Report {
    let ws = Workspace::new(t.path()).unwrap();
    let agent = Agent { config, ..Agent::new(model, &ws, gate) };
    agent.run(TASK).await
}

/// The text of every tool result in the conversation.
fn results(report: &Report) -> Vec<&str> {
    report.transcript.iter().filter(|m| m.role == Role::Tool).map(|m| m.content.as_str()).collect()
}

fn users(report: &Report) -> Vec<&str> {
    report.transcript.iter().filter(|m| m.role == Role::User).map(|m| m.content.as_str()).collect()
}

/// The work state board of a request: the lines from `[state]` to the end of its last message.
fn board(request: &Recorded) -> Option<&str> {
    let last = request.messages.last()?;
    last.content.find("[state] step").map(|at| &last.content[at..])
}

/// A request's messages as the stored history has them, which is without the board.
fn stored(request: &Recorded) -> Vec<Message> {
    let mut msgs = request.messages.clone();
    let Some(last) = msgs.last_mut() else { return msgs };
    if last.content.starts_with("[state] step") {
        msgs.pop();
    } else if let Some(at) = last.content.find("\n\n[state] step") {
        last.content.truncate(at);
    }
    msgs
}

/// Writes down the events of a run, one short line each.
#[derive(Default)]
struct Recorder(Mutex<Vec<String>>);

impl Recorder {
    fn lines(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }

    fn count(&self, line: &str) -> usize {
        self.0.lock().unwrap().iter().filter(|l| l.as_str() == line).count()
    }
}

impl Sink for Recorder {
    fn event(&self, event: &Event<'_>) {
        let line = match event {
            Event::Step(n) => format!("step:{n}"),
            Event::Context(_) => return,
            Event::Delta(text) => format!("delta:{text}"),
            Event::Text(text) => format!("text:{text}"),
            Event::Answer(text) => format!("answer:{text}"),
            Event::Call { name, .. } => format!("call:{name}"),
            Event::Result { name, .. } => format!("result:{name}"),
            Event::Gate(_) => "gate".to_string(),
            Event::Nudge(_) => "nudge".to_string(),
            Event::Compacted { .. } => "compacted".to_string(),
        };
        self.0.lock().unwrap().push(line);
    }
}

/// A model that takes its time before each reply.
struct Slow<'a>(&'a ScriptedModel, Duration);

impl Model for Slow<'_> {
    async fn complete(&self, req: Request<'_>) -> Result<Reply, ModelError> {
        tokio::time::sleep(self.1).await;
        self.0.complete(req).await
    }
}

/// A model that never answers.
struct Never;

impl Model for Never {
    async fn complete(&self, _: Request<'_>) -> Result<Reply, ModelError> {
        std::future::pending().await
    }
}

// ---- what a request holds ----

#[tokio::test]
async fn each_request_starts_with_the_stored_history_of_the_one_before() {
    let t = project();
    let model = ScriptedModel::new([call("read_file", READ), say("The function already works."), call("edit_file", FIX), say("Fixed.")]);
    let r = run_with(&t, &model, &adds(), Config::default()).await;
    assert_eq!((r.status.clone(), r.rejections), (Status::Verified, 1));
    let requests = model.requests();
    assert_eq!(requests.len(), 4);
    assert!(board(&requests[0]).is_none());
    for pair in requests.windows(2) {
        let (earlier, later) = (stored(&pair[0]), stored(&pair[1]));
        assert!(later.len() > earlier.len());
        // Only the board differs, and it sits at the very end: a server that caches prompt
        // prefixes can reuse everything before it.
        assert_eq!(later[..earlier.len()], earlier[..]);
        assert!(board(&pair[1]).is_some());
    }
    // After a nudge the board rides on that message; after a tool result it is a message of its own.
    let after_nudge = requests[2].messages.last().unwrap();
    assert!(after_nudge.content.starts_with("The work is not finished") && after_nudge.content.contains("\n\n[state] step 3 of 30"));
    assert!(requests[3].messages.last().unwrap().content.starts_with("[state] step 4 of 30"));
    assert!(r.transcript.iter().all(|m| !m.content.contains("[state] step")));
    assert_eq!(stored(&requests[3])[..], r.transcript[..r.transcript.len() - 1]);
}

#[tokio::test]
async fn the_board_says_where_the_work_stands() {
    let t = project();
    let model = ScriptedModel::new([call("read_file", READ), call("edit_file", FIX), say("Fixed.")]);
    let r = run_with(&t, &model, &adds(), Config::default()).await;
    assert_eq!(r.status, Status::Verified);
    let requests = model.requests();
    assert_eq!(board(&requests[1]), Some("[state] step 2 of 30\nread: calc.py\ntests: not run yet"));
    assert_eq!(
        board(&requests[2]),
        Some(
            "[state] step 3 of 30\nchanged: calc.py\nread: calc.py\ntests: pass\nnext: the tests pass: say in one sentence that the work is finished"
        )
    );
}

#[tokio::test]
async fn files_the_task_names_are_read_before_the_first_call() {
    let t = project();
    let line_edit = r#"{"path": "calc.py", "start": 2, "end": 2, "new": "    return a + b"}"#;
    let model = ScriptedModel::new([call("read_file", READ), call("replace_lines", line_edit), say("Fixed.")]);
    let ws = Workspace::new(t.path()).unwrap();
    let gate = adds();
    let agent = Agent::new(&model, &ws, &gate);
    let r = agent.run("Fix add() in calc.py.").await;
    assert_eq!((r.status.clone(), r.steps), (Status::Verified, 3));
    let first = users(&r)[0];
    let page = "Files named in the task below (already read for you):\ncalc.py lines 1-2 of 2\n1| def add(a, b):\n2|     return a - b";
    assert!(first.contains(page), "{first}");
    assert!(first.find("Project files").unwrap() < first.find(page).unwrap());
    assert!(first.ends_with("Task:\nFix add() in calc.py."));
    // The file is in view from the start: reading it again is refused, a line edit goes through.
    let results = results(&r);
    assert!(results[0].starts_with("You already have this part of the file above"), "{}", results[0]);
    assert!(results[1].starts_with("calc.py: lines 2-2 replaced (1 -> 1 lines). Now:\n"), "{}", results[1]);
    assert_eq!(t.read("calc.py"), "def add(a, b):\n    return a + b\n");
    let requests = model.requests();
    assert!(board(&requests[1]).unwrap().contains("\nread: calc.py\n"));
    assert!(board(&requests[2]).unwrap().contains("\nchanged: calc.py (line 2)\n"));
}

#[tokio::test]
async fn project_instructions_and_the_code_map_open_the_first_message() {
    let t = project();
    t.write("AGENTS.md", "Use four spaces.\n");
    let model = ScriptedModel::new([say("It subtracts instead of adding.")]);
    let r = run_with(&t, &model, &Gate::None, Config::default()).await;
    assert_eq!(r.status, Status::Answered);
    let first = users(&r)[0];
    assert!(
        first.starts_with("Project instructions (AGENTS.md):\nUse four spaces.\n\nProject files (path: what is defined in it):\n"),
        "{first}"
    );
    assert!(first.lines().any(|l| l.starts_with("calc.py: ") && l.contains("add")), "{first}");
    assert!(first.ends_with(&format!("Task:\n{TASK}")));
}

// ---- conversations ----

#[tokio::test]
async fn a_session_keeps_the_conversation_across_turns() {
    let t = project();
    let model = ScriptedModel::new([
        call("read_file", READ),
        call("edit_file", FIX),
        say("Fixed add()."),
        call("write_file", r#"{"path": "util.py", "content": "def twice(x):\n    return 2 * x\n"}"#),
        say("Added util.py."),
        say("It doubles a number."),
    ]);
    let ws = Workspace::new(t.path()).unwrap();
    let gate = adds();
    let agent = Agent::new(&model, &ws, &gate);
    let mut session = agent.session();

    let first = session.turn(TASK).await;
    assert_eq!((first.status.clone(), first.steps), (Status::Verified, 3));
    assert_eq!(first.changed, ["calc.py"]);

    // The second turn counts for itself and continues the same history.
    let second = session.turn("  Now add util.py with twice().\n").await;
    assert_eq!((second.status.clone(), second.steps), (Status::Verified, 2));
    assert_eq!(second.changed, ["util.py"]);
    assert_eq!(second.text, "Added util.py.");
    let said = users(&second);
    assert_eq!(said.len(), 2);
    assert_eq!(said[1], "Now add util.py with twice().");
    assert_eq!(said.iter().filter(|m| m.contains("Project files")).count(), 1);
    let fourth = &model.requests()[3];
    assert_eq!(fourth.messages.last().unwrap().content, "Now add util.py with twice().");
    assert_eq!(fourth.messages.len(), first.transcript.len() + 1);
    assert!(session.tokens() > 0 && session.messages().len() == second.transcript.len());

    // A new agent (other settings) picks the conversation up where it stopped.
    let history = session.into_history();
    let kept = history.len();
    let talk = Agent { config: Config { check_answers: false, ..Config::default() }, ..Agent::new(&model, &ws, &gate) };
    let mut resumed = talk.resume(history);
    let third = resumed.turn("What does twice do?").await;
    assert_eq!((third.status.clone(), third.steps), (Status::Answered, 1));
    assert_eq!(third.text, "It doubles a number.");
    assert!(third.changed.is_empty());
    let last = &model.requests()[5];
    assert_eq!(last.messages.len(), kept + 1);
    assert_eq!(last.messages[0].role, Role::System);
    assert_eq!(last.messages.iter().filter(|m| m.role == Role::System).count(), 1);
    assert_eq!(last.messages.last().unwrap().content, "What does twice do?");
}

#[tokio::test]
async fn an_answer_in_a_conversation_does_not_start_the_tests() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model = ScriptedModel::new([say("add() subtracts instead of adding.")]);
    let r = run_with(&t, &model, &counted(runs.clone()), Config { check_answers: false, ..Config::default() }).await;
    assert_eq!(r.status, Status::Answered);
    assert_eq!(runs.load(Ordering::Relaxed), 0);
}

// ---- interrupts ----

#[tokio::test]
async fn a_stop_before_the_turn_ends_it_at_once_and_a_reset_clears_it() {
    let t = project();
    let model = ScriptedModel::new([call("edit_file", FIX), say("Fixed.")]);
    let ws = Workspace::new(t.path()).unwrap();
    let gate = adds();
    let cancel = Cancel::new();
    cancel.cancel();
    let agent = Agent { cancel: Some(&cancel), ..Agent::new(&model, &ws, &gate) };
    let r = agent.run(TASK).await;
    assert_eq!((r.status.clone(), r.steps, model.calls()), (Status::Interrupted, 0, 0));
    assert_eq!(Status::Interrupted.as_str(), "interrupted");

    cancel.reset();
    let r = agent.run(TASK).await;
    assert_eq!(r.status, Status::Verified);
}

struct StopOnCall<'a>(&'a Cancel);

impl Sink for StopOnCall<'_> {
    fn event(&self, event: &Event<'_>) {
        if matches!(event, Event::Call { .. }) {
            self.0.cancel();
        }
    }
}

#[tokio::test]
async fn a_stop_during_a_tool_call_leaves_a_well_formed_history() {
    let t = project();
    let model = ScriptedModel::new([calls(&[("edit_file", FIX), ("read_file", READ)])]);
    let ws = Workspace::new(t.path()).unwrap();
    let gate = adds();
    let cancel = Cancel::new();
    let sink = StopOnCall(&cancel);
    let agent = Agent { cancel: Some(&cancel), sink: &sink, ..Agent::new(&model, &ws, &gate) };
    let r = agent.run(TASK).await;
    assert_eq!((r.status.clone(), model.calls()), (Status::Interrupted, 1));
    // Every call of the reply has its result, so the conversation can go on later.
    assert_eq!(results(&r), ["[interrupted]", "[interrupted]"]);
    assert_eq!(t.read("calc.py"), BROKEN);
}

#[tokio::test]
async fn a_stop_while_the_model_writes_ends_the_call() {
    let t = project();
    let ws = Workspace::new(t.path()).unwrap();
    let gate = adds();
    let cancel = Cancel::new();
    let model = Never;
    let agent = Agent { cancel: Some(&cancel), ..Agent::new(&model, &ws, &gate) };
    let stop = async {
        tokio::time::sleep(Duration::from_millis(50)).await;
        cancel.cancel();
    };
    let (r, ()) = tokio::join!(agent.run(TASK), stop);
    assert_eq!((r.status.clone(), r.steps), (Status::Interrupted, 1));
    let last = r.transcript.last().unwrap();
    assert!(last.role == Role::Assistant && last.content == "(interrupted)");
}

// ---- tests the harness starts on its own ----

#[tokio::test]
async fn a_change_brings_its_test_result_along() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model = ScriptedModel::new([call("edit_file", FIX), say("Fixed.")]);
    let r = run_with(&t, &model, &counted(runs.clone()), Config::default()).await;
    assert_eq!((r.status.clone(), r.rejections), (Status::Verified, 0));
    assert!(results(&r)[0].ends_with("\n[tests after this change: all pass]"), "{}", results(&r)[0]);
    // The claim is judged by that same run.
    assert_eq!(runs.load(Ordering::Relaxed), 1);
}

#[tokio::test]
async fn a_change_that_breaks_the_tests_says_so_in_its_result() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model = ScriptedModel::new([
        call("edit_file", r#"{"path": "calc.py", "old": "    return a - b", "new": "    return a * b"}"#),
        say("Done."),
        call("edit_file", r#"{"path": "calc.py", "old": "    return a * b", "new": "    return a + b"}"#),
        say("Fixed."),
    ]);
    let r = run_with(&t, &model, &counted(runs.clone()), Config::default()).await;
    assert_eq!((r.status.clone(), r.rejections), (Status::Verified, 1));
    let results = results(&r);
    assert!(
        results[0].ends_with("\n[tests after this change: fail]\nFAILED tests/test_calc.py::test_add - assert -1 == 3"),
        "{}",
        results[0]
    );
    assert!(results[1].ends_with("\n[tests after this change: all pass]"));
    // One run per change; the false claim in between is answered from the run already made.
    assert_eq!(runs.load(Ordering::Relaxed), 2);
    let requests = model.requests();
    let told = board(&requests[1]).unwrap();
    assert!(told.contains("\ntests: fail\nnext: fix the cause of the failing tests, then call run_tests"), "{told}");
}

#[tokio::test]
async fn without_eager_tests_the_claim_starts_them() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model = ScriptedModel::new([call("edit_file", FIX), say("Fixed.")]);
    let r = run_with(&t, &model, &counted(runs.clone()), Config { eager_tests: false, ..Config::default() }).await;
    assert_eq!(r.status, Status::Verified);
    assert!(!results(&r)[0].contains("[tests after"));
    assert_eq!(runs.load(Ordering::Relaxed), 1);
    let requests = model.requests();
    let told = board(&requests[1]).unwrap();
    assert!(told.ends_with("tests: not run since the last change\nnext: call run_tests"), "{told}");
}

#[tokio::test]
async fn slow_tests_keep_running_while_the_model_writes() {
    let t = project();
    // About two seconds, which is longer than the round waits for them.
    let cmd = if cfg!(windows) { "echo x>>runs.log& ping -n 3 127.0.0.1 >nul" } else { "echo x >> runs.log; sleep 2" };
    let gate = Gate::command(cmd);
    let script = ScriptedModel::new([call("edit_file", FIX), say("Fixed.")]);
    let model = Slow(&script, Duration::from_millis(1200));
    let ws = Workspace::new(t.path()).unwrap();
    let sink = Recorder::default();
    let agent = Agent { sink: &sink, ..Agent::new(&model, &ws, &gate) };
    let r = agent.run(TASK).await;
    assert_eq!(r.status, Status::Verified);
    assert!(!results(&r)[0].contains("[tests after"));
    assert_eq!(board(&script.requests()[1]).unwrap().lines().find(|l| l.starts_with("tests:")), Some("tests: running"));
    // The run the harness started is the one that judges the claim.
    assert_eq!(t.read("runs.log").lines().count(), 1);
    assert_eq!(sink.count("gate"), 1);
}

// ---- tool sets ----

#[tokio::test]
async fn with_strict_tools_the_work_ends_with_done() {
    let t = project();
    let model = ScriptedModel::new([
        call("done", r#"{"summary": "All good."}"#),
        call("edit_file", FIX),
        calls(&[("done", r#"{"summary": "Fixed add."}"#), ("read_file", READ)]),
    ]);
    let r = run_with(&t, &model, &adds(), Config { strict_tools: true, ..Config::default() }).await;
    assert_eq!((r.status.clone(), r.rejections), (Status::Verified, 1));
    assert_eq!(r.text, "Fixed add.");
    let requests = model.requests();
    assert_eq!(requests[0].tool_choice, ToolChoice::Required);
    assert_eq!(requests[0].tools.last().map(String::as_str), Some("done"));
    assert!(requests[0].messages[0].content.contains("call done"));
    let results = results(&r);
    assert!(results[0].starts_with("The work is not finished: the tests fail.\nFAILED"), "{}", results[0]);
    assert_eq!(results[2..], ["done", "[skipped: the work was already declared finished]"]);
    assert!(board(&requests[2]).unwrap().ends_with("next: the tests pass: call done with one sentence on what you did"));

    // Without the setting the model may answer in text and `done` is not offered.
    let plain = ScriptedModel::new([say("It subtracts.")]);
    run_with(&t, &plain, &Gate::None, Config::default()).await;
    let first = &plain.requests()[0];
    assert_eq!(first.tool_choice, ToolChoice::Auto);
    assert!(!first.tools.iter().any(|name| name == "done"));
}

#[tokio::test]
async fn a_note_stays_on_the_board() {
    let t = project();
    let model = ScriptedModel::new([call("note", r#"{"text": "add  must keep\nits signature"}"#), call("note", "{}"), say("Noted.")]);
    let r = run_with(&t, &model, &Gate::None, Config { profile: Some(Profile::Full), ..Config::default() }).await;
    assert_eq!(r.status, Status::Answered);
    assert_eq!(results(&r), ["noted", "note needs a text: one short fact worth keeping."]);
    let requests = model.requests();
    assert_eq!(requests[0].tools, ["read_file", "list_files", "search", "replace_lines", "edit_file", "write_file", "run", "note"]);
    // No tests in this project, so the board has no tests line.
    assert_eq!(board(&requests[1]), Some("[state] step 2 of 30\nnotes: add must keep its signature"));
    assert!(board(&requests[2]).unwrap().ends_with("notes: add must keep its signature"));
}

#[tokio::test]
async fn a_skill_is_listed_by_name_and_read_on_request() {
    let t = project();
    t.write(".cap/skills/release/SKILL.md", "---\nname: release\ndescription: Cut a release.\n---\nBump the version.\n");
    let ws = Workspace::new(t.path()).unwrap();
    let skills = skills::load(ws.root(), None);
    assert_eq!(skills.len(), 1);
    let model = ScriptedModel::new([
        call("use_skill", r#"{"name": "nope"}"#),
        call("use_skill", r#"{"name": "release"}"#),
        say("Read the release steps."),
    ]);
    let agent = Agent { skills: &skills, ..Agent::new(&model, &ws, &Gate::None) };
    let r = agent.run("How do we release?").await;
    assert_eq!(r.status, Status::Answered);
    let first = users(&r)[0];
    assert!(
        first.contains("\n\nSkills (call use_skill with a name to get its instructions):\n- release: Cut a release.\n\nTask:\n"),
        "{first}"
    );
    assert!(model.requests()[0].tools.iter().any(|name| name == "use_skill"));
    assert!(model.requests()[0].messages[0].content.contains("use_skill"));
    assert_eq!(
        results(&r),
        [
            "There is no skill named nope. The skills are: release.",
            "Skill release:\nBump the version.\n[the skill's own files are in .cap/skills/release]"
        ]
    );

    // Without skills the tool does not exist.
    let bare = ScriptedModel::new([call("use_skill", r#"{"name": "release"}"#), say("No skills here.")]);
    let r = Agent::new(&bare, &ws, &Gate::None).run("How do we release?").await;
    assert!(results(&r)[0].starts_with("There is no tool named use_skill."), "{}", results(&r)[0]);
}

// ---- tools of outside servers ----

/// An MCP server with one tool that sends its text argument back.
async fn echo_server(stream: DuplexStream) {
    let (reader, mut writer) = tokio::io::split(stream);
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let message: Value = serde_json::from_str(&line).unwrap();
        let (Some(method), Some(id)) = (message["method"].as_str(), message.get("id")) else { continue };
        let result = match method {
            "initialize" => {
                json!({"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}})
            }
            "tools/list" => json!({"tools": [{
                "name": "echo",
                "description": "Send the text back.",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            }]}),
            "tools/call" => json!({"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}]}),
            _ => Value::Null,
        };
        let reply = json!({"jsonrpc": "2.0", "id": id, "result": result});
        writer.write_all(format!("{reply}\n").as_bytes()).await.unwrap();
    }
}

async fn hub(allow: bool) -> McpHub {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    tokio::spawn(echo_server(theirs));
    let (reader, writer) = tokio::io::split(ours);
    let mut hub = McpHub::default();
    assert_eq!(hub.add("srv", allow, Client::over(Box::new(BufReader::new(reader)), Box::new(writer))).await, Ok(1));
    hub
}

const ECHO: &str = r#"{"text": "hello from outside"}"#;

#[tokio::test]
async fn an_outside_tool_needs_approval_unless_its_server_is_trusted() {
    let t = project();
    let ws = Workspace::new(t.path()).unwrap();
    let asks = hub(false).await;

    // Nobody approves: the call is refused and nothing ran.
    let model = ScriptedModel::new([call("nope", "{}"), call("mcp__srv__echo", ECHO), say("The echo tool was not approved.")]);
    let agent = Agent { mcp: Some(&asks), ..Agent::new(&model, &ws, &Gate::None) };
    let r = agent.run("Call the echo tool.").await;
    assert_eq!(r.status, Status::Answered);
    let tools = &model.requests()[0].tools;
    assert_eq!(tools[tools.len() - 2..], ["run", "mcp__srv__echo"]);
    let results = results(&r);
    assert!(results[0].ends_with("run, and 1 more from outside servers (their names start with mcp__)."), "{}", results[0]);
    assert_eq!(
        results[1],
        "Not approved: it calls a tool of an outside server (MCP). Go on without this step, or finish and say what is left."
    );

    // A person approves: it runs, and the run counts as an action nobody checked.
    let model = ScriptedModel::new([call("mcp__srv__echo", ECHO), say("Called it.")]);
    let agent = Agent { mcp: Some(&asks), approver: &AllowAll, ..Agent::new(&model, &ws, &Gate::None) };
    let r = agent.run("Call the echo tool.").await;
    assert_eq!(r.status, Status::Unverified);
    assert_eq!(self::results(&r), ["hello from outside"]);

    // The server is marked as trusted in the settings: no question is asked.
    let trusted = hub(true).await;
    let model = ScriptedModel::new([call("mcp__srv__echo", ECHO), say("Called it.")]);
    let agent = Agent { mcp: Some(&trusted), approver: &DenyAll, ..Agent::new(&model, &ws, &Gate::None) };
    let r = agent.run("Call the echo tool.").await;
    assert_eq!((r.status.clone(), self::results(&r)), (Status::Unverified, vec!["hello from outside"]));
}

// ---- streaming ----

#[tokio::test]
async fn text_reaches_the_sink_while_it_arrives() {
    let t = project();
    let ws = Workspace::new(t.path()).unwrap();
    let script = || ScriptedModel::new([Reply { content: "Looking at the file.".into(), ..call("read_file", READ) }, say("It subtracts.")]);

    let (model, sink) = (script(), Recorder::default());
    let agent = Agent { sink: &sink, ..Agent::new(&model, &ws, &Gate::None) };
    assert_eq!(agent.run(TASK).await.status, Status::Answered);
    assert_eq!(
        sink.lines(),
        [
            "step:1",
            "delta:Looking at the file.",
            "text:Looking at the file.",
            "call:read_file",
            "result:read_file",
            "step:2",
            "delta:It subtracts.",
            "answer:It subtracts.",
            "gate"
        ]
    );

    let (model, sink) = (script(), Recorder::default());
    let agent = Agent { sink: &sink, config: Config { stream: false, ..Config::default() }, ..Agent::new(&model, &ws, &Gate::None) };
    assert_eq!(agent.run(TASK).await.status, Status::Answered);
    assert!(!sink.lines().iter().any(|l| l.starts_with("delta:")));
    assert_eq!(sink.count("answer:It subtracts."), 1);
}

// ---- a small window ----

#[tokio::test]
async fn what_left_the_context_no_longer_counts_as_read() {
    let t = TempDir::new("compact");
    let names = ["a", "b", "c", "d", "e", "f", "g", "h"];
    for name in names {
        let text: String = (1..=60).map(|n| format!("{}_VALUE_{n} = {n}  # keep this line as it is\n", name.to_uppercase())).collect();
        t.write(&format!("{name}.py"), &text);
    }
    let line_edit = r#"{"path": "a.py", "start": 2, "end": 2, "new": "A_VALUE_2 = 0"}"#;
    let mut script: Vec<Reply> = names.iter().map(|name| call("read_file", &format!(r#"{{"path": "{name}.py"}}"#))).collect();
    script.extend([call("replace_lines", line_edit), call("replace_lines", line_edit), say("Set it to 0.")]);
    let model = ScriptedModel::new(script);
    let ws = Workspace::new(t.path()).unwrap();
    let sink = Recorder::default();
    let config = Config { context_window: 6000, max_output: 1024, ..Config::default() };
    let agent = Agent { sink: &sink, config, ..Agent::new(&model, &ws, &Gate::None) };
    let r = agent.run("Set the second value of the first module to 0.").await;
    assert_eq!(r.status, Status::Unverified, "{}", r.text);
    assert!(sink.count("compacted") >= 1);

    // The oldest read was dropped from the context to make room.
    let results = results(&r);
    assert!(results[0].starts_with(STUB), "{}", results[0]);
    // So the board stops listing the file, and a line edit there is held back until its lines are shown again.
    let requests = model.requests();
    let before_edit = board(&requests[8]).unwrap();
    let read_line = before_edit.lines().find(|l| l.starts_with("read:")).unwrap();
    assert!(!read_line.contains("a.py") && read_line.ends_with("h.py"), "{read_line}");
    assert!(results[8].starts_with("a.py: you have not read lines 2-2 as they are now"), "{}", results[8]);
    assert!(results[9].starts_with("a.py: lines 2-2 replaced"), "{}", results[9]);
    assert!(t.read("a.py").starts_with("A_VALUE_1 = 1  # keep this line as it is\nA_VALUE_2 = 0\nA_VALUE_3 = 3"));
}
