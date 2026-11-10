//! Keeping a request inside the model's context window.
//!
//! Token counts are estimated from characters and corrected by what the server reports.
//! When the history is too large, old tool outputs are replaced by one-line stubs first,
//! because a small model rarely needs a file listing from ten steps ago.

use crate::model::{Message, Role, ToolSpec};

/// Messages at the end of the history that are never stubbed.
pub const KEEP_RECENT: usize = 6;
/// Smallest reply size the loop still tries to work with.
pub const MIN_OUTPUT: u32 = 512;
/// Tokens left unused so an estimate that is slightly low does not overflow the window.
pub const MARGIN: u32 = 600;
const STUB: &str = "[dropped to save context: ";

/// Rough token count of a text: ASCII is about 3.5 characters per token, other scripts cost more.
pub fn estimate_text(text: &str) -> u32 {
    let (mut ascii, mut wide) = (0u32, 0u32);
    for c in text.chars() {
        if c.is_ascii() { ascii += 1 } else { wide += 1 }
    }
    (ascii as f32 / 3.5 + wide as f32 * 1.5).ceil() as u32
}

pub fn estimate_message(m: &Message) -> u32 {
    let calls: u32 = m.tool_calls.iter().map(|c| estimate_text(&c.name) + estimate_text(&c.args) + 8).sum();
    estimate_text(&m.content) + calls + 6
}

pub fn estimate_messages(msgs: &[Message]) -> u32 {
    msgs.iter().map(estimate_message).sum()
}

pub fn estimate_tools(tools: &[ToolSpec]) -> u32 {
    tools.iter().map(|t| estimate_text(t.name) + estimate_text(t.description) + estimate_text(&t.parameters.to_string()) + 12).sum()
}

/// Correction factor learned from the server's own count of the last prompt.
#[derive(Clone, Copy, Debug)]
pub struct Calibration(pub f32);

impl Default for Calibration {
    fn default() -> Self {
        Calibration(1.0)
    }
}

impl Calibration {
    pub fn apply(self, estimate: u32) -> u32 {
        (estimate as f32 * self.0).ceil() as u32
    }

    /// Move toward `reported / estimated`, never below 0.7 or above 2.0.
    pub fn learn(&mut self, estimated: u32, reported: u32) {
        if estimated < 200 || reported == 0 {
            return;
        }
        let seen = reported as f32 / estimated as f32;
        self.0 = (self.0 * 0.5 + seen * 0.5).clamp(0.7, 2.0);
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fit {
    /// Output tokens the next call may use.
    pub max_tokens: u32,
    pub stubbed: usize,
    pub dropped: usize,
    /// Estimated prompt size after fitting.
    pub prompt: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Overflow {
    pub prompt: u32,
    pub window: u32,
}

fn stub_of(content: &str) -> String {
    let first: String = content.lines().next().unwrap_or("").chars().take(120).collect();
    format!("{STUB}{first}]")
}

/// Shrink `msgs` in place until prompt + reply fit in `window`.
///
/// Order: stub old tool outputs, then give the reply less room, then drop the oldest
/// assistant/tool rounds. The system prompt and the first user message always stay.
pub fn fit(msgs: &mut Vec<Message>, tools_tokens: u32, window: u32, want_output: u32, cal: Calibration) -> Result<Fit, Overflow> {
    let size = |msgs: &[Message]| cal.apply(estimate_messages(msgs) + tools_tokens);
    let room = window.saturating_sub(MARGIN);
    let mut out = Fit { max_tokens: want_output, stubbed: 0, dropped: 0, prompt: size(msgs) };
    if out.prompt + want_output <= room {
        return Ok(out);
    }

    // Stub down to a lower mark than strictly needed, so the next few calls reuse the same prefix.
    let target = (room.saturating_sub(want_output) as f32 * 0.8) as u32;
    let last_stubbable = msgs.len().saturating_sub(KEEP_RECENT);
    for i in 0..last_stubbable {
        if out.prompt <= target {
            break;
        }
        let m = &mut msgs[i];
        if m.role == Role::Tool && !m.content.starts_with(STUB) && m.content.len() > 200 {
            m.content = stub_of(&m.content);
            out.stubbed += 1;
            out.prompt = size(msgs);
        }
    }
    if out.prompt + want_output <= room {
        return Ok(out);
    }

    if out.prompt + MIN_OUTPUT <= room {
        out.max_tokens = room - out.prompt;
        return Ok(out);
    }

    // Drop whole rounds (an assistant message and the tool results that answer it).
    let head = msgs.iter().position(|m| m.role == Role::User).map_or(1, |i| i + 1).min(msgs.len());
    while out.prompt + MIN_OUTPUT > room {
        let Some(start) = (head..msgs.len().saturating_sub(2)).find(|&i| msgs[i].role == Role::Assistant) else { break };
        let end = (start + 1..msgs.len()).find(|&i| msgs[i].role != Role::Tool).unwrap_or(msgs.len());
        if end >= msgs.len() {
            break;
        }
        out.dropped += end - start;
        msgs.drain(start..end);
        out.prompt = size(msgs);
    }
    if out.prompt + MIN_OUTPUT > room {
        return Err(Overflow { prompt: out.prompt, window });
    }
    out.max_tokens = want_output.min(room - out.prompt);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ToolCall;

    fn round(i: usize, body: &str) -> [Message; 2] {
        let id = format!("c{i}");
        [
            Message::assistant("", vec![ToolCall { id: id.clone(), name: "read_file".into(), args: format!(r#"{{"path":"f{i}.py"}}"#) }]),
            Message::tool(id, format!("f{i}.py lines 1-200 of 200\n{body}")),
        ]
    }

    fn history(rounds: usize, body_chars: usize) -> Vec<Message> {
        let mut msgs = vec![Message::system("You are a coding agent."), Message::user("Fix the bug.")];
        let body = "x = 1\n".repeat(body_chars / 6);
        for i in 0..rounds {
            msgs.extend(round(i, &body));
        }
        msgs
    }

    #[test]
    fn estimate_counts_korean_heavier_than_ascii() {
        assert_eq!(estimate_text(""), 0);
        assert_eq!(estimate_text("abcdefg"), 2);
        assert!(estimate_text("테스트를 고친다") > estimate_text("fix the test"));
    }

    #[test]
    fn small_history_is_left_alone() {
        let mut msgs = history(2, 600);
        let before = msgs.clone();
        let fit = fit(&mut msgs, 300, 16_384, 2048, Calibration::default()).unwrap();
        assert_eq!((fit.stubbed, fit.dropped, fit.max_tokens), (0, 0, 2048));
        assert_eq!(msgs, before);
    }

    #[test]
    fn old_tool_outputs_are_stubbed_and_recent_ones_kept() {
        let mut msgs = history(10, 7000);
        let fit = fit(&mut msgs, 300, 16_384, 2048, Calibration::default()).unwrap();
        assert!(fit.stubbed > 0 && fit.dropped == 0);
        assert!(fit.prompt + fit.max_tokens <= 16_384 - MARGIN);
        assert_eq!(msgs[3].content, "[dropped to save context: f0.py lines 1-200 of 200]");
        let n = msgs.len();
        assert!(msgs[n - 1].content.len() > 5000, "the newest tool output stays whole");
        // Hysteresis: fitting again right away changes nothing, so the prefix is stable.
        let snapshot = msgs.clone();
        let again = super::fit(&mut msgs, 300, 16_384, 2048, Calibration::default()).unwrap();
        assert_eq!(again.stubbed, 0);
        assert_eq!(msgs, snapshot);
    }

    #[test]
    fn output_shrinks_before_rounds_are_dropped() {
        // Three large recent rounds that may not be stubbed: 3 × ~2000 tokens in an 8k window.
        let mut msgs = history(3, 7000);
        let fit = fit(&mut msgs, 300, 8192, 2048, Calibration::default()).unwrap();
        assert_eq!(fit.dropped, 0);
        assert!(fit.max_tokens >= MIN_OUTPUT && fit.max_tokens < 2048);
    }

    #[test]
    fn rounds_are_dropped_when_nothing_else_helps_and_the_task_stays() {
        let mut msgs = history(3, 7000);
        let fit = fit(&mut msgs, 300, 5500, 2048, Calibration::default()).unwrap();
        assert!(fit.dropped >= 2);
        assert_eq!(msgs[1].content, "Fix the bug.");
        assert!(fit.prompt + fit.max_tokens <= 5500 - MARGIN);
        // Every tool message still follows the assistant message that called it.
        for (i, m) in msgs.iter().enumerate() {
            if m.role == Role::Tool {
                assert!(matches!(msgs[i - 1].role, Role::Assistant | Role::Tool));
            }
        }
    }

    #[test]
    fn a_task_larger_than_the_window_is_an_overflow() {
        let mut msgs = vec![Message::system("s"), Message::user("x ".repeat(20_000))];
        assert!(fit(&mut msgs, 0, 4096, 2048, Calibration::default()).is_err());
    }

    #[test]
    fn calibration_follows_the_server_count_within_bounds() {
        let mut cal = Calibration::default();
        cal.learn(1000, 1300);
        assert!((cal.0 - 1.15).abs() < 1e-3);
        cal.learn(1000, 90_000);
        assert_eq!(cal.0, 2.0);
        cal.learn(50, 500);
        assert_eq!(cal.0, 2.0, "tiny prompts teach nothing");
        assert_eq!(Calibration(1.5).apply(100), 150);
    }
}
