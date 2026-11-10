//! The whole loop, driven by a scripted model in a temporary project.

use cap_core::agent::{Agent, Config, Report, Status};
use cap_core::gate::{self, Gate, Verdict};
use cap_core::model::{ErrorKind, Role};
use cap_core::perm::{AllowAll, Approver, DenyAll, Mode};
use cap_core::testing::{ScriptedModel, TempDir, call, calls, cut, say};
use cap_core::workspace::Workspace;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

const BROKEN: &str = "def add(a, b):\n    return a - b\n";
const FIX: &str = r#"{"path": "calc.py", "old": "    return a - b", "new": "    return a + b"}"#;

fn project() -> TempDir {
    let t = TempDir::new("loop");
    t.write("calc.py", BROKEN).write("tests/test_calc.py", "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n");
    t
}

/// Passes once `calc.py` adds. Stands in for the project's tests so the scenarios need no Python.
fn adds() -> Gate {
    counted(Arc::new(AtomicU32::new(0)))
}

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

async fn run_with(t: &TempDir, model: &ScriptedModel, gate: &Gate, config: Config, approver: &dyn Approver) -> Report {
    let ws = Workspace::new(t.path()).unwrap();
    let agent = Agent { approver, config, ..Agent::new(model, &ws, gate) };
    agent.run("add() returns the wrong result. Fix it.").await
}

async fn run(t: &TempDir, model: &ScriptedModel, gate: &Gate) -> Report {
    run_with(t, model, gate, Config::default(), &DenyAll).await
}

/// The text of every tool result in the conversation.
fn results(report: &Report) -> Vec<&str> {
    report.transcript.iter().filter(|m| m.role == Role::Tool).map(|m| m.content.as_str()).collect()
}

fn users(report: &Report) -> Vec<&str> {
    report.transcript.iter().filter(|m| m.role == Role::User).map(|m| m.content.as_str()).collect()
}

#[tokio::test]
async fn a_fix_that_passes_the_tests_is_verified() {
    let t = project();
    let model =
        ScriptedModel::new([call("read_file", r#"{"path": "calc.py"}"#), call("edit_file", FIX), say("Fixed add() to return a + b.")]);
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::Verified);
    assert_eq!((r.steps, r.rejections), (3, 0));
    assert_eq!(r.changed, ["calc.py"]);
    assert_eq!(r.text, "Fixed add() to return a + b.");
    assert!(t.read("calc.py").contains("a + b"));
    // The first message carries the task and the file list, so the model can start without a listing call.
    let first = &model.requests()[0];
    assert!(first.messages[1].content.contains("Fix it.") && first.messages[1].content.contains("tests/test_calc.py"));
    assert_eq!(first.tools, ["read_file", "search", "replace_lines", "write_file", "run_tests", "run"]);
}

#[tokio::test]
async fn a_false_done_is_rejected_and_the_model_gets_the_failure() {
    let t = project();
    let model = ScriptedModel::new([say("The function already works."), call("edit_file", FIX), say("Fixed.")]);
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::Verified);
    assert_eq!(r.rejections, 1);
    let second = &model.requests()[1];
    let told = &second.messages.last().unwrap().content;
    assert!(told.contains("not finished") && told.contains("test_add"), "{told}");
}

#[tokio::test]
async fn a_model_that_only_claims_never_gets_verified() {
    let t = project();
    let model = ScriptedModel::repeating(say("Everything is fixed and all tests pass."));
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::TestsFail);
    // The same failure three times ends the run.
    assert_eq!((model.calls(), r.rejections), (3, 3));
    assert!(r.failure.as_deref().unwrap().contains("test_add"));
    assert_eq!(t.read("calc.py"), BROKEN);
}

#[tokio::test]
async fn different_failures_stop_at_the_verify_limit() {
    let t = project();
    let n = Arc::new(AtomicU32::new(0));
    let gate = Gate::Custom(Box::new(move |_: &Path| {
        let out = format!("FAILED test_case_{}", ["a", "b", "c", "d", "e"][n.fetch_add(1, Ordering::Relaxed) as usize % 5]);
        Verdict::Fail { signature: gate::signature(&out), tail: out }
    }));
    let model = ScriptedModel::repeating(say("Done."));
    let r = run(&t, &model, &gate).await;
    assert_eq!((r.status, r.rejections), (Status::TestsFail, 4));
}

#[tokio::test]
async fn run_tests_is_answered_by_the_gate_and_a_fresh_pass_is_reused() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model = ScriptedModel::new([
        call("run_tests", "{}"),
        call("edit_file", FIX),
        call("run_tests", ""),
        call("run", r#"{"command": "git status"}"#),
        say("Fixed."),
    ]);
    let r = run(&t, &model, &counted(runs.clone())).await;
    assert_eq!(r.status, Status::Verified);
    let out = results(&r);
    assert!(out[0].starts_with("run_tests: tests fail") && out[0].contains("test_add"));
    assert_eq!(out[2], "run_tests: all tests pass");
    // Looking around does not make the pass stale: the "done" claim costs no third test run.
    assert_eq!(runs.load(Ordering::Relaxed), 2);
}

#[tokio::test]
async fn a_change_after_the_last_test_run_is_tested_again() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let undo = r#"{"path": "calc.py", "old": "    return a + b", "new": "    return a - b"}"#;
    let model = ScriptedModel::new([call("edit_file", FIX), call("run_tests", "{}"), call("edit_file", undo), say("Fixed.")]);
    let r = run_with(&t, &model, &counted(runs.clone()), Config { max_verify: 1, ..Config::default() }, &DenyAll).await;
    assert_eq!(r.status, Status::TestsFail);
    assert_eq!(runs.load(Ordering::Relaxed), 2);
}

#[tokio::test]
async fn writing_outside_the_project_is_refused() {
    let t = project();
    let outside = t.path().parent().unwrap().join("cap-loop-escape.txt");
    let _ = std::fs::remove_file(&outside);
    let model = ScriptedModel::new([
        call("write_file", r#"{"path": "../cap-loop-escape.txt", "content": "x"}"#),
        call("write_file", &serde_json::json!({"path": outside.to_str().unwrap(), "content": "x"}).to_string()),
        say("Could not write there."),
    ]);
    let r = run_with(&t, &model, &Gate::None, Config { mode: Mode::Auto, ..Config::default() }, &AllowAll).await;
    assert!(!outside.exists());
    assert!(results(&r).iter().all(|o| o.starts_with("Refused:")), "{:?}", results(&r));
    assert_eq!(r.status, Status::Answered);
    assert!(r.changed.is_empty());
}

#[tokio::test]
async fn secrets_stay_out_of_the_conversation() {
    let t = project();
    t.write(".env", "API_KEY=sk-live-0123456789\n");
    let model = ScriptedModel::new([
        call("read_file", r#"{"path": ".env"}"#),
        call("run", r#"{"command": "cat .env"}"#),
        call("search", r#"{"pattern": "API_KEY"}"#),
        say("I cannot read that file."),
    ]);
    let r = run_with(&t, &model, &Gate::None, Config { mode: Mode::Auto, ..Config::default() }, &AllowAll).await;
    for m in &r.transcript {
        assert!(!m.content.contains("sk-live"), "{}", m.content);
    }
    assert!(!users(&r)[0].contains(".env"));
}

#[tokio::test]
async fn refusals_in_a_row_end_the_run() {
    let t = project();
    let model = ScriptedModel::repeating(call("read_file", r#"{"path": ".env"}"#));
    t.write(".env", "K=v\n");
    let r = run(&t, &model, &adds()).await;
    assert!(matches!(r.status, Status::Stuck(_)), "{:?}", r.status);
    assert_eq!(model.calls(), 3);
}

#[tokio::test]
async fn changing_an_existing_test_needs_a_yes() {
    let weaken = r#"{"path": "tests/test_calc.py", "old": "    assert add(1, 2) == 3", "new": "    assert True"}"#;
    let t = project();
    let model = ScriptedModel::new([call("edit_file", weaken), say("Done.")]);
    let r = run(&t, &model, &Gate::None).await;
    assert!(results(&r)[0].starts_with("Not approved:"), "{}", results(&r)[0]);
    assert!(t.read("tests/test_calc.py").contains("== 3"));

    let t = project();
    let model = ScriptedModel::new([call("edit_file", weaken), say("Done.")]);
    let r = run_with(&t, &model, &Gate::None, Config::default(), &AllowAll).await;
    assert_eq!(r.changed, ["tests/test_calc.py"]);
    // Changed files and no tests to judge them: done, and marked as unverified.
    assert_eq!(r.status, Status::Unverified);
}

#[tokio::test]
async fn names_and_arguments_from_other_agents_still_work() {
    let t = project();
    let model = ScriptedModel::new([
        call("Read", r#"{"file_path": "calc.py"}"#),
        call("str_replace_editor", r#"{"file_path": "calc.py", "old_string": "    return a - b", "new_string": "    return a + b"}"#),
        say("Fixed."),
    ]);
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::Verified);
}

#[tokio::test]
async fn broken_arguments_are_explained_and_the_next_try_counts() {
    let t = project();
    let model = ScriptedModel::new([
        call("edit_file", r#"{"path": "calc.py", "old": "    return a - b", "new": "#),
        call("edit_file", r#"["calc.py"]"#),
        call("make_coffee", "{}"),
        say("Done."),
    ]);
    let r = run(&t, &model, &adds()).await;
    let out = results(&r);
    assert!(out[0].contains("not valid JSON"), "{}", out[0]);
    assert!(out[1].contains("one JSON object"), "{}", out[1]);
    assert!(out[2].contains("no tool named make_coffee") && out[2].contains("replace_lines"), "{}", out[2]);
    // Three rounds without one usable call end the run.
    assert!(matches!(r.status, Status::Stuck(_)), "{:?}", r.status);
    assert_eq!(model.calls(), 3);

    let t = project();
    let model = ScriptedModel::new([call("edit_file", "{oops"), call("edit_file", FIX), say("Fixed.")]);
    assert_eq!(run(&t, &model, &adds()).await.status, Status::Verified);
}

#[tokio::test]
async fn the_same_failing_call_is_cut_short() {
    let t = project();
    let wrong = r#"{"path": "calc.py", "old": "    return a * b", "new": "    return a + b"}"#;
    let model = ScriptedModel::new([
        call("edit_file", wrong),
        call("list_files", "{}"),
        call("edit_file", wrong),
        call("list_files", "{}"),
        call("edit_file", wrong),
        say("Done."),
    ]);
    let r = run_with(&t, &model, &Gate::None, Config::default(), &DenyAll).await;
    let out = results(&r);
    assert!(out[0].contains("calc.py") && out[2] == out[0]);
    assert!(out[4].contains("failed twice"), "{}", out[4]);
}

#[tokio::test]
async fn an_unchanged_file_is_not_read_twice() {
    let t = project();
    let read = r#"{"path": "calc.py"}"#;
    let model = ScriptedModel::new([
        call("read_file", read),
        call("read_file", read),
        call("edit_file", FIX),
        call("read_file", read),
        say("Fixed."),
    ]);
    let r = run(&t, &model, &adds()).await;
    let out = results(&r);
    assert!(out[0].contains("return a - b"));
    assert!(out[1].contains("already have"), "{}", out[1]);
    // After the edit the file is new again.
    assert!(out[3].contains("return a + b"), "{}", out[3]);
    assert_eq!(r.status, Status::Verified);
}

#[tokio::test]
async fn saying_what_comes_next_is_not_a_done_claim() {
    let t = project();
    let runs = Arc::new(AtomicU32::new(0));
    let model =
        ScriptedModel::new([say("Let me read the file first."), say("먼저 파일을 수정하겠습니다."), call("edit_file", FIX), say("Fixed.")]);
    let r = run(&t, &model, &counted(runs.clone())).await;
    assert_eq!((r.status, r.rejections), (Status::Verified, 0));
    assert_eq!(runs.load(Ordering::Relaxed), 1);
    assert!(model.requests()[1].messages.last().unwrap().content.contains("Call the tool now"));
}

#[tokio::test]
async fn empty_and_cut_off_replies_get_a_few_more_tries() {
    let t = project();
    let model = ScriptedModel::new([say(""), cut("def add(a, b):\n    return"), call("edit_file", FIX), say("Fixed.")]);
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::Verified);
    assert!(users(&r).iter().any(|u| u.contains("was empty")) && users(&r).iter().any(|u| u.contains("cut off")));

    let t = project();
    let r = run(&t, &ScriptedModel::repeating(say("  ")), &adds()).await;
    assert!(matches!(r.status, Status::Stuck(_)));
    let t = project();
    let model = ScriptedModel::repeating(cut(&"x".repeat(5000)));
    let r = run(&t, &model, &adds()).await;
    assert!(matches!(r.status, Status::Stuck(_)));
    assert_eq!(model.calls(), 3);
    // A cut-off reply is kept short in the history.
    assert!(r.transcript.iter().all(|m| m.role != Role::Assistant || m.content.chars().count() < 900));
}

#[tokio::test]
async fn the_step_limit_ends_a_run_that_goes_nowhere() {
    let t = project();
    let model = ScriptedModel::repeating(call("list_files", "{}"));
    let r = run_with(&t, &model, &adds(), Config { max_steps: 5, ..Config::default() }, &DenyAll).await;
    assert_eq!((r.status, r.steps, model.calls()), (Status::StepLimit, 5, 5));
}

#[tokio::test]
async fn a_server_error_ends_the_run_with_its_kind() {
    let t = project();
    let model = ScriptedModel::new([call("read_file", r#"{"path": "calc.py"}"#)]);
    model.push_error(ErrorKind::Auth, "401");
    let r = run(&t, &model, &adds()).await;
    assert_eq!(r.status, Status::ModelFailed(ErrorKind::Auth));
    assert_eq!(r.steps, 2);
}

#[tokio::test]
async fn read_mode_offers_no_way_to_change_files() {
    let t = project();
    let model = ScriptedModel::new([
        call("edit_file", FIX),
        call("run", r#"{"command": "python fix.py"}"#),
        say("add() subtracts instead of adding."),
    ]);
    let r = run_with(&t, &model, &Gate::None, Config { mode: Mode::Read, ..Config::default() }, &DenyAll).await;
    assert_eq!(model.requests()[0].tools, ["read_file", "list_files", "search", "run"]);
    assert!(results(&r)[0].contains("no tool named edit_file"));
    assert_eq!(t.read("calc.py"), BROKEN);
    assert_eq!(r.status, Status::Answered);
}

#[tokio::test]
async fn several_calls_in_one_reply_all_run() {
    let t = project();
    let model =
        ScriptedModel::new([calls(&[("read_file", r#"{"path": "calc.py"}"#), ("edit_file", FIX), ("run_tests", "{}")]), say("Fixed.")]);
    let r = run(&t, &model, &adds()).await;
    assert_eq!((&r.status, r.steps), (&Status::Verified, 2));
    assert_eq!(results(&r).len(), 3);
}

#[tokio::test]
async fn a_long_run_stays_inside_a_small_window() {
    let t = project();
    t.write("big.py", &"value = compute(alpha, beta, gamma)  # filler line\n".repeat(200));
    let mut script = Vec::new();
    for start in [1, 41, 81, 121, 161] {
        script.push(call("read_file", &format!(r#"{{"path": "big.py", "start": {start}, "limit": 40}}"#)));
    }
    script.push(call("edit_file", FIX));
    script.push(say("Fixed."));
    let model = ScriptedModel::new(script);
    let r = run_with(&t, &model, &adds(), Config { context_window: 3000, ..Config::default() }, &DenyAll).await;
    assert_eq!(r.status, Status::Verified);
    let last = model.requests().pop().unwrap();
    // The task is still there, and old file contents have been shortened.
    assert!(last.messages[1].content.contains("Fix it."));
    assert!(last.messages.iter().any(|m| m.content.starts_with("[dropped to save context")) || last.messages.len() < 14);
}
