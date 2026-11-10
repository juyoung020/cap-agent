//! The project folder: resolving the paths a model writes, and walking the files.

use std::io;
use std::path::{Component, Path, PathBuf};

/// Folders that hold build output, caches or tool state, never the code being worked on.
pub const SKIP_DIRS: &[&str] = &[
    ".git",
    ".cap",
    "target",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".tox",
    ".next",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub abs: PathBuf,
    /// Path from the project root with `/` separators; `.` for the root itself.
    pub rel: String,
}

#[derive(Clone, Debug)]
pub struct Workspace {
    root: PathBuf,
}

/// `\\?\C:\x` as `C:\x`. `cmd.exe` refuses the long form as a working directory.
fn simplify(p: PathBuf) -> PathBuf {
    match p.to_str().and_then(|s| s.strip_prefix(r"\\?\")) {
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => PathBuf::from(rest),
        _ => p,
    }
}

fn real(p: &Path) -> io::Result<PathBuf> {
    p.canonicalize().map(simplify)
}

impl Workspace {
    pub fn new(root: impl AsRef<Path>) -> io::Result<Self> {
        let root = real(root.as_ref())?;
        if !root.is_dir() {
            return Err(io::Error::new(io::ErrorKind::NotADirectory, format!("{} is not a folder", root.display())));
        }
        Ok(Self { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Where `raw` points, or `None` when that is outside the project.
    ///
    /// `..` is folded first; then the deepest part that exists is replaced by its real path,
    /// so a link that leads out of the project is caught as well.
    pub fn resolve(&self, raw: &str) -> Option<Resolved> {
        let raw = raw.trim().trim_matches(['"', '\'', '`']).replace('\\', "/");
        let raw = raw.strip_prefix("./").unwrap_or(&raw);
        let given = Path::new(if raw.is_empty() { "." } else { raw });
        let joined = if given.is_absolute() { given.to_path_buf() } else { self.root.join(given) };

        let mut folded = PathBuf::new();
        for part in joined.components() {
            match part {
                Component::CurDir => {}
                Component::ParentDir => {
                    if !folded.pop() {
                        return None;
                    }
                }
                other => folded.push(other),
            }
        }

        let mut existing = folded.as_path();
        let mut missing: Vec<&std::ffi::OsStr> = Vec::new();
        let base = loop {
            if let Ok(found) = real(existing) {
                break found;
            }
            missing.push(existing.file_name()?);
            existing = existing.parent()?;
        };
        let abs = missing.iter().rev().fold(base, |p, name| p.join(name));
        let inside = abs.strip_prefix(&self.root).ok()?;
        let parts: Vec<String> = inside.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
        let rel = if parts.is_empty() { ".".to_string() } else { parts.join("/") };
        Some(Resolved { abs, rel })
    }

    /// Files under `dir` (a path from the root), sorted, as paths from the root.
    /// Returns at most `limit` and whether more exist.
    pub fn files(&self, dir: &Path, limit: usize) -> (Vec<String>, bool) {
        let mut out = Vec::new();
        let mut stack = vec![dir.to_path_buf()];
        while let Some(d) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&d) else { continue };
            let mut entries: Vec<_> = entries.flatten().collect();
            entries.sort_by_key(|e| e.file_name());
            let mut dirs = Vec::new();
            for e in entries {
                let name = e.file_name().to_string_lossy().into_owned();
                let Ok(kind) = e.file_type() else { continue };
                if kind.is_symlink() {
                    continue;
                }
                if kind.is_dir() {
                    if !SKIP_DIRS.contains(&name.as_str()) && !name.ends_with(".egg-info") {
                        dirs.push(e.path());
                    }
                } else if !name.ends_with(".pyc") {
                    if out.len() == limit {
                        return (out, true);
                    }
                    if let Ok(inside) = e.path().strip_prefix(&self.root) {
                        out.push(inside.components().map(|c| c.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/"));
                    }
                }
            }
            // Depth first, in name order.
            stack.extend(dirs.into_iter().rev());
        }
        (out, false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::TempDir;

    #[test]
    fn paths_inside_the_project_resolve() {
        let t = TempDir::new("ws");
        t.write("src/a.py", "x = 1\n");
        let ws = Workspace::new(t.path()).unwrap();
        assert_eq!(ws.resolve("src/a.py").unwrap().rel, "src/a.py");
        assert_eq!(ws.resolve("./src\\a.py").unwrap().rel, "src/a.py");
        assert_eq!(ws.resolve("src/../src/./a.py").unwrap().rel, "src/a.py");
        assert_eq!(ws.resolve("\"src/a.py\"").unwrap().rel, "src/a.py");
        assert_eq!(ws.resolve("").unwrap().rel, ".");
        assert_eq!(ws.resolve("new/deep/file.py").unwrap().rel, "new/deep/file.py");
        let abs = ws.root().join("src").join("a.py");
        assert_eq!(ws.resolve(abs.to_str().unwrap()).unwrap().rel, "src/a.py");
        assert!(!ws.root().to_string_lossy().starts_with(r"\\?\"));
    }

    #[test]
    fn paths_outside_the_project_do_not_resolve() {
        let t = TempDir::new("ws");
        let ws = Workspace::new(t.path()).unwrap();
        assert_eq!(ws.resolve("../outside.txt"), None);
        assert_eq!(ws.resolve("src/../../outside.txt"), None);
        let other = std::env::temp_dir().join("elsewhere.txt");
        assert_eq!(ws.resolve(other.to_str().unwrap()), None);
        assert_eq!(ws.resolve(if cfg!(windows) { "C:\\Windows\\win.ini" } else { "/etc/passwd" }), None);
    }

    #[test]
    fn walking_skips_caches_and_keeps_order() {
        let t = TempDir::new("ws");
        t.write("b.py", "").write("a/z.py", "").write("a/y.py", "").write(".git/config", "").write("node_modules/x/i.js", "");
        t.write("__pycache__/a.pyc", "").write("target/debug/x", "");
        let ws = Workspace::new(t.path()).unwrap();
        assert_eq!(ws.files(ws.root(), 100), (vec!["b.py".to_string(), "a/y.py".into(), "a/z.py".into()], false));
        assert_eq!(ws.files(ws.root(), 2), (vec!["b.py".to_string(), "a/y.py".into()], true));
        assert_eq!(ws.files(&ws.root().join("a"), 100).0, vec!["a/y.py", "a/z.py"]);
    }
}
