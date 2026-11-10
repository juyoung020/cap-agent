//! The work state board: a few lines at the end of each request that say where the work stands.
//!
//! A small model loses track of what it already changed once the history grows or old tool
//! outputs are stubbed. The harness knows these facts exactly, so it states them instead of
//! leaving the model to reconstruct them. The board is added to the request copy only; the
//! stored history stays free of it, which keeps the prompt prefix stable between calls.

/// The whole board stays under this many characters.
pub const MAX_CHARS: usize = 400;
const MAX_CHANGED: usize = 6;
const MAX_READ: usize = 8;
const NOTE_CHARS: usize = 90;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tests {
    Pass,
    Fail,
    /// The harness started them after the last change and they have not finished.
    Running,
    /// A file changed after the last run.
    Stale,
    NotRun,
}

pub struct Board<'a> {
    pub step: u32,
    pub max_steps: u32,
    /// Files changed in this turn, with the line range of the last line edit when it is known.
    pub changed: &'a [(String, Option<(usize, usize)>)],
    /// Files read so far, oldest first.
    pub read: &'a [String],
    /// `None` when the project has no tests to run.
    pub tests: Option<Tests>,
    pub notes: &'a [String],
    /// The work ends with the `done` tool instead of a plain answer.
    pub done_tool: bool,
}

fn clip(text: &str, max: usize) -> String {
    let one_line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if one_line.chars().count() <= max {
        return one_line;
    }
    let cut: String = one_line.chars().take(max.saturating_sub(3)).collect();
    format!("{cut}...")
}

fn list(items: Vec<String>, shown: usize) -> String {
    let extra = items.len().saturating_sub(shown);
    let mut text = items.into_iter().take(shown).collect::<Vec<_>>().join(", ");
    if extra > 0 {
        text.push_str(&format!(" (+{extra} more)"));
    }
    text
}

impl Board<'_> {
    /// The one step that is clearly next, when there is one.
    fn next(&self) -> Option<&'static str> {
        let has_changes = !self.changed.is_empty();
        if self.max_steps.saturating_sub(self.step) < 3 {
            return Some("few steps are left: finish now and say what is done and what is left");
        }
        match self.tests? {
            Tests::Fail => Some("fix the cause of the failing tests, then call run_tests"),
            Tests::Stale | Tests::Running if has_changes => Some("call run_tests"),
            Tests::Pass if has_changes && self.done_tool => Some("the tests pass: call done with one sentence on what you did"),
            Tests::Pass if has_changes => Some("the tests pass: say in one sentence that the work is finished"),
            _ => None,
        }
    }

    pub fn render(&self) -> String {
        let head = format!("[state] step {} of {}", self.step, self.max_steps);
        let changed = (!self.changed.is_empty()).then(|| {
            let items = self
                .changed
                .iter()
                .map(|(path, range)| match range {
                    Some((a, b)) if a == b => format!("{path} (line {a})"),
                    Some((a, b)) => format!("{path} (lines {a}-{b})"),
                    None => path.clone(),
                })
                .collect();
            format!("changed: {}", list(items, MAX_CHANGED))
        });
        let read = (!self.read.is_empty()).then(|| {
            // The newest reads are the ones still in view.
            let from = self.read.len().saturating_sub(MAX_READ);
            let mut text = self.read[from..].join(", ");
            if from > 0 {
                text = format!("(+{from} earlier) {text}");
            }
            format!("read: {text}")
        });
        let tests = self.tests.map(|t| {
            let state = match t {
                Tests::Pass => "pass",
                Tests::Fail => "fail",
                Tests::Running => "running",
                Tests::Stale => "not run since the last change",
                Tests::NotRun => "not run yet",
            };
            format!("tests: {state}")
        });
        let notes = (!self.notes.is_empty())
            .then(|| format!("notes: {}", self.notes.iter().map(|n| clip(n, NOTE_CHARS)).collect::<Vec<_>>().join("; ")));
        let next = self.next().map(|n| format!("next: {n}"));

        let join = |read: &Option<String>, notes: &Option<String>| {
            [Some(head.clone()), changed.clone(), read.clone(), tests.clone(), notes.clone(), next.clone()]
                .into_iter()
                .flatten()
                .collect::<Vec<_>>()
                .join("\n")
        };
        let full = join(&read, &notes);
        if full.chars().count() <= MAX_CHARS {
            return full;
        }
        // Too long: the read list goes first, then the notes shrink.
        let without_read = join(&None, &notes);
        if without_read.chars().count() <= MAX_CHARS {
            return without_read;
        }
        let over = without_read.chars().count() - MAX_CHARS;
        let shorter = notes.as_ref().map(|n| clip(n, n.chars().count().saturating_sub(over).max(20)));
        let text = join(&None, &shorter);
        if text.chars().count() <= MAX_CHARS { text } else { join(&None, &None).chars().take(MAX_CHARS).collect() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn board<'a>(
        changed: &'a [(String, Option<(usize, usize)>)],
        read: &'a [String],
        tests: Option<Tests>,
        notes: &'a [String],
    ) -> Board<'a> {
        Board { step: 4, max_steps: 30, changed, read, tests, notes, done_tool: false }
    }

    #[test]
    fn the_board_lists_what_the_harness_knows() {
        let changed = [("calc.py".to_string(), Some((3, 4))), ("util.py".to_string(), None), ("one.py".to_string(), Some((7, 7)))];
        let read = ["calc.py".to_string(), "tests/test_calc.py".to_string()];
        let notes = ["add  must\nkeep its signature".to_string()];
        let text = board(&changed, &read, Some(Tests::Stale), &notes).render();
        assert_eq!(
            text,
            "[state] step 4 of 30\nchanged: calc.py (lines 3-4), util.py, one.py (line 7)\nread: calc.py, tests/test_calc.py\n\
             tests: not run since the last change\nnotes: add must keep its signature\nnext: call run_tests"
        );
    }

    #[test]
    fn the_next_line_follows_the_test_state() {
        let changed = [("a.py".to_string(), None)];
        let next = |tests, changed: &[(String, Option<(usize, usize)>)]| {
            board(changed, &[], tests, &[]).render().lines().find(|l| l.starts_with("next:")).map(str::to_string)
        };
        assert!(next(Some(Tests::Fail), &changed).unwrap().contains("fix the cause"));
        assert!(next(Some(Tests::Pass), &changed).unwrap().contains("the work is finished"));
        assert_eq!(next(Some(Tests::Running), &changed).unwrap(), "next: call run_tests");
        assert_eq!(next(Some(Tests::NotRun), &[]), None);
        assert_eq!(next(Some(Tests::Pass), &[]), None);
        assert_eq!(next(None, &changed), None);
        // Without a gate there is no tests line at all.
        assert!(!board(&changed, &[], None, &[]).render().contains("tests:"));
        let late = Board { step: 28, max_steps: 30, changed: &changed, read: &[], tests: Some(Tests::Fail), notes: &[], done_tool: false };
        assert!(late.render().contains("next: few steps are left"));
        let strict = Board { step: 4, max_steps: 30, changed: &changed, read: &[], tests: Some(Tests::Pass), notes: &[], done_tool: true };
        assert!(strict.render().contains("next: the tests pass: call done"));
    }

    #[test]
    fn a_crowded_board_stays_short_and_keeps_the_next_line() {
        let changed: Vec<_> = (0..12).map(|i| (format!("src/module_{i}/handler.py"), Some((10, 20)))).collect();
        let read: Vec<_> = (0..30).map(|i| format!("src/module_{i}/handler.py")).collect();
        let notes: Vec<_> = (0..5).map(|i| format!("note {i}: {}", "word ".repeat(40))).collect();
        let text = board(&changed, &read, Some(Tests::Fail), &notes).render();
        assert!(text.chars().count() <= MAX_CHARS, "{} chars", text.chars().count());
        assert!(text.contains("(+6 more)") && text.contains("next: fix the cause"));
        assert!(!text.contains("read:"));
        // With room to spare the read list shows its newest entries.
        let roomy = board(&[], &read, None, &[]).render();
        assert!(roomy.contains("(+22 earlier)") && roomy.ends_with("src/module_29/handler.py"));
    }
}
