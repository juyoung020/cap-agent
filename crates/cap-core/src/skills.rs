//! Skills: folders with a `SKILL.md` that hold instructions for one kind of task.
//!
//! The format is the one other agents use (`.claude/skills`, `.agents/skills`), so skills a
//! project already has work here unchanged. Only the name and one line of description go into
//! the prompt; the body is read when the model asks for it with `use_skill`.

use std::path::{Path, PathBuf};

/// Skills listed in the prompt.
pub const MAX_LISTED: usize = 30;
const DESCRIPTION_CHARS: usize = 200;
const MAX_SKILL_BYTES: u64 = 200_000;
/// Folders searched under the project and under the home folder, in this order.
pub const SKILL_DIRS: &[&str] = &[".cap/skills", ".claude/skills", ".agents/skills"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Skill {
    pub name: String,
    pub description: String,
    /// The instructions: `SKILL.md` without its front matter.
    pub body: String,
    /// The skill's folder, which may hold files the instructions refer to.
    pub dir: PathBuf,
}

fn unquote(value: &str) -> &str {
    let v = value.trim();
    for q in ['"', '\''] {
        if v.len() >= 2 && v.starts_with(q) && v.ends_with(q) {
            return &v[1..v.len() - 1];
        }
    }
    v
}

/// Split a `SKILL.md` into name, description and body. Without front matter the folder name
/// is the name and the first line of text is the description.
pub fn parse(text: &str, fallback_name: &str) -> (String, String, String) {
    let text = text.trim_start_matches('\u{feff}').replace("\r\n", "\n");
    let mut name = String::new();
    let mut description = String::new();
    let mut body = text.as_str();
    if let Some(rest) = text.strip_prefix("---\n")
        && let Some(end) = rest.find("\n---")
    {
        let after = &rest[end + 4..];
        body = after.split_once('\n').map_or("", |(_, b)| b);
        // The key whose value continues on the following indented lines.
        let mut open: Option<&str> = None;
        for line in rest[..end].lines() {
            let indented = line.starts_with([' ', '\t']);
            if indented && let Some(key) = open {
                let target = if key == "name" { &mut name } else { &mut description };
                if !target.is_empty() {
                    target.push(' ');
                }
                target.push_str(line.trim());
                continue;
            }
            open = None;
            let Some((key, value)) = line.split_once(':') else { continue };
            let key = key.trim();
            if key != "name" && key != "description" {
                continue;
            }
            let value = value.trim();
            let folded = matches!(value, ">" | "|" | ">-" | "|-" | ">+" | "|+" | "");
            let target = if key == "name" { &mut name } else { &mut description };
            *target = if folded { String::new() } else { unquote(value).to_string() };
            open = Some(if key == "name" { "name" } else { "description" });
        }
    }
    let body = body.trim().to_string();
    if name.trim().is_empty() {
        name = fallback_name.to_string();
    }
    if description.trim().is_empty() {
        description = body.lines().map(|l| l.trim_start_matches('#').trim()).find(|l| !l.is_empty()).unwrap_or("").to_string();
    }
    (name.trim().to_string(), description.trim().to_string(), body)
}

fn load_dir(dir: &Path, out: &mut Vec<Skill>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let mut folders: Vec<PathBuf> = entries.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
    folders.sort();
    for folder in folders {
        let file = folder.join("SKILL.md");
        if std::fs::metadata(&file).map_or(true, |m| !m.is_file() || m.len() > MAX_SKILL_BYTES) {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&file) else { continue };
        let fallback = folder.file_name().and_then(|n| n.to_str()).unwrap_or("skill");
        let (name, description, body) = parse(&text, fallback);
        if body.is_empty() || out.iter().any(|s| s.name.eq_ignore_ascii_case(&name)) {
            continue;
        }
        out.push(Skill { name, description, body, dir: folder });
    }
}

/// The skills of a project and of the user. A project skill wins over a user skill of the same name.
pub fn load(project: &Path, home: Option<&Path>) -> Vec<Skill> {
    let mut out = Vec::new();
    for base in std::iter::once(project).chain(home) {
        for dir in SKILL_DIRS {
            load_dir(&base.join(dir), &mut out);
        }
    }
    out
}

pub fn find<'a>(skills: &'a [Skill], name: &str) -> Option<&'a Skill> {
    let want = name.trim().trim_start_matches('/');
    skills.iter().find(|s| s.name.eq_ignore_ascii_case(want))
}

/// `- name: description` lines for the prompt.
pub fn listing(skills: &[Skill]) -> String {
    let mut lines: Vec<String> = skills
        .iter()
        .take(MAX_LISTED)
        .map(|s| {
            let d: String = s.description.split_whitespace().collect::<Vec<_>>().join(" ");
            let d = if d.chars().count() > DESCRIPTION_CHARS {
                format!("{}...", d.chars().take(DESCRIPTION_CHARS - 3).collect::<String>())
            } else {
                d
            };
            if d.is_empty() { format!("- {}", s.name) } else { format!("- {}: {}", s.name, d) }
        })
        .collect();
    if skills.len() > MAX_LISTED {
        lines.push(format!("(+{} more)", skills.len() - MAX_LISTED));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::TempDir;

    #[test]
    fn front_matter_gives_the_name_and_description() {
        let text = "---\nname: \"release\"\ndescription: Cut a release.\nallowed-tools: Bash\n---\n\n# Release\n\n1. Bump the version.\n";
        assert_eq!(parse(text, "dir"), ("release".into(), "Cut a release.".into(), "# Release\n\n1. Bump the version.".into()));
        // A folded description continues on indented lines.
        let folded = "---\r\nname: review\r\ndescription: >\r\n  Review a change\r\n  for bugs.\r\nversion: 1\r\n---\r\nRead the diff.\r\n";
        assert_eq!(parse(folded, "dir"), ("review".into(), "Review a change for bugs.".into(), "Read the diff.".into()));
        // Without front matter the folder names the skill and the first line describes it.
        assert_eq!(
            parse("# Deploy steps\n\nRun the script.\n", "deploy"),
            ("deploy".into(), "Deploy steps".into(), "# Deploy steps\n\nRun the script.".into())
        );
    }

    #[test]
    fn project_skills_come_before_user_skills_of_the_same_name() {
        let project = TempDir::new("skills-p");
        let home = TempDir::new("skills-h");
        project.write(".claude/skills/release/SKILL.md", "---\nname: release\ndescription: project one\n---\nSteps A\n");
        project.write(".cap/skills/lint/SKILL.md", "---\ndescription: Run the linter\n---\nSteps L\n");
        project.write(".cap/skills/empty/SKILL.md", "---\nname: empty\n---\n");
        project.write(".cap/skills/notes.md", "not a skill");
        home.write(".agents/skills/release/SKILL.md", "---\nname: Release\ndescription: user one\n---\nSteps B\n");
        home.write(".claude/skills/deploy/SKILL.md", "Deploy it.\n");
        let skills = load(project.path(), Some(home.path()));
        let names: Vec<&str> = skills.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["lint", "release", "deploy"]);
        assert_eq!(find(&skills, "RELEASE").unwrap().description, "project one");
        assert_eq!(find(&skills, "/deploy").unwrap().body, "Deploy it.");
        assert!(find(&skills, "empty").is_none() && find(&skills, "nope").is_none());
        assert_eq!(listing(&skills), "- lint: Run the linter\n- release: project one\n- deploy: Deploy it.");
        assert!(load(project.path(), None).len() == 2);
    }

    #[test]
    fn the_listing_is_bounded() {
        let many: Vec<Skill> = (0..40)
            .map(|i| Skill { name: format!("s{i}"), description: "word ".repeat(100), body: "b".into(), dir: PathBuf::new() })
            .collect();
        let text = listing(&many);
        assert_eq!(text.lines().count(), MAX_LISTED + 1);
        assert!(text.ends_with("(+10 more)"));
        assert!(text.lines().next().unwrap().chars().count() <= DESCRIPTION_CHARS + 10);
    }
}
