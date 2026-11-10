//! The code map: the functions and types each source file defines, shown next to the file list.
//!
//! A model that sees `calc.py: add, sub, Stack(push, pop)` knows where to look without a search step.
//! The outline comes from scanning lines, so it works on files that do not parse.

use crate::context::estimate_text;
use std::path::Path;

/// Symbols listed for one file.
pub const MAX_SYMBOLS: usize = 30;
/// Members listed for one class, trait or impl block.
pub const MAX_MEMBERS: usize = 12;
/// Larger files are listed by name only.
pub const MAX_FILE_BYTES: u64 = 200_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Symbol {
    pub name: String,
    pub members: Vec<String>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Lang {
    Python,
    Rust,
    Script,
    Go,
    Managed,
    C,
}

fn lang_of(path: &str) -> Option<Lang> {
    let ext = path.rsplit_once('.')?.1.to_ascii_lowercase();
    Some(match ext.as_str() {
        "py" | "pyi" => Lang::Python,
        "rs" => Lang::Rust,
        "js" | "jsx" | "ts" | "tsx" | "mjs" | "cjs" => Lang::Script,
        "go" => Lang::Go,
        "java" | "kt" | "kts" | "cs" => Lang::Managed,
        "c" | "h" | "cc" | "cpp" | "cxx" | "hpp" | "hh" => Lang::C,
        _ => return None,
    })
}

/// What one line declares.
enum Found {
    /// A function, constant or type without members worth listing.
    Item(String),
    /// A class, trait, impl block or module: the symbols inside it are its members.
    Container(String),
    /// A method written outside its type (`func (t *T) name()` in Go): member `1` of container `0`.
    MemberOf(String, String),
    /// A block whose contents are left out (a Rust `mod tests`).
    Hidden,
}

fn ident(text: &str) -> Option<String> {
    let name: String = text.chars().take_while(|c| c.is_alphanumeric() || *c == '_' || *c == '$').collect();
    (!name.is_empty() && !name.chars().next().is_some_and(|c| c.is_ascii_digit())).then_some(name)
}

/// The identifier right before the first `(`, when the line reads like a declaration.
fn name_before_paren(line: &str) -> Option<String> {
    let head = line.split_once('(')?.0.trim_end();
    if head.contains('=') || head.is_empty() {
        return None;
    }
    let start = head.rfind(|c: char| !(c.is_alphanumeric() || c == '_' || c == '~' || c == ':')).map_or(0, |i| i + 1);
    let name = head[start..].trim_matches(':');
    const NOT_NAMES: &[&str] = &[
        "if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "do", "sizeof", "typeof", "await", "yield", "using",
        "lock", "foreach", "when", "super", "this", "delete", "assert", "defined",
    ];
    let first_word = head.split_whitespace().next().unwrap_or("");
    if name.is_empty()
        || NOT_NAMES.contains(&name)
        || NOT_NAMES.contains(&first_word)
        || name.chars().next().is_some_and(|c| c.is_ascii_digit())
    {
        return None;
    }
    Some(name.to_string())
}

fn strip_words<'a>(mut line: &'a str, words: &[&str]) -> &'a str {
    loop {
        let before = line;
        for w in words {
            if let Some(rest) = line.strip_prefix(w).filter(|r| r.starts_with([' ', '\t'])) {
                line = rest.trim_start();
            }
        }
        if line.len() == before.len() {
            return line;
        }
    }
}

fn after<'a>(line: &'a str, keyword: &str) -> Option<&'a str> {
    line.strip_prefix(keyword).filter(|r| r.starts_with([' ', '\t'])).map(str::trim_start)
}

fn python(line: &str) -> Option<Found> {
    let line = strip_words(line, &["async"]);
    if let Some(rest) = after(line, "def") {
        let name = ident(rest)?;
        return (!name.starts_with("__")).then_some(Found::Item(name));
    }
    after(line, "class").and_then(ident).map(Found::Container)
}

fn rust(line: &str) -> Option<Found> {
    let mut line = line;
    if let Some(rest) = line.strip_prefix("pub") {
        line = match rest.strip_prefix('(') {
            Some(scoped) => scoped.split_once(')')?.1.trim_start(),
            None if rest.starts_with(' ') => rest.trim_start(),
            None => line,
        };
    }
    if let Some(rest) = after(line, "impl").or_else(|| line.strip_prefix("impl<")) {
        // `impl<T> Trait for Type<T> {` names `Type`.
        let rest = if line.starts_with("impl<") { rest.split_once('>').map_or(rest, |(_, r)| r.trim_start()) } else { rest };
        let target = rest.split(" where ").next().unwrap_or(rest);
        let target = target.rsplit_once(" for ").map_or(target, |(_, t)| t).trim_start_matches('&').trim();
        return ident(target.rsplit("::").next().unwrap_or(target)).map(Found::Container);
    }
    if let Some(rest) = after(line, "mod") {
        if line.trim_end().ends_with(';') {
            return None;
        }
        let name = ident(rest)?;
        return Some(if name == "tests" || name == "test" { Found::Hidden } else { Found::Container(name) });
    }
    if let Some(rest) = after(line, "trait") {
        return ident(rest).map(Found::Container);
    }
    if let Some(rest) = line.strip_prefix("macro_rules!") {
        return ident(rest.trim_start()).map(Found::Item);
    }
    let line = strip_words(line, &["default", "async", "unsafe", "extern \"C\"", "extern"]);
    let line = if line.starts_with("const fn") || line.starts_with("const unsafe") || line.starts_with("const async") {
        strip_words(line, &["const", "unsafe", "async"])
    } else {
        line
    };
    for keyword in ["fn", "struct", "enum", "union", "type", "const", "static"] {
        if let Some(rest) = after(line, keyword) {
            let name = ident(rest.strip_prefix("mut ").unwrap_or(rest))?;
            return (name != "_").then_some(Found::Item(name));
        }
    }
    None
}

fn script(line: &str, inside: bool) -> Option<Found> {
    let line = strip_words(line, &["export", "default", "declare", "abstract", "async"]);
    if let Some(rest) = line.strip_prefix("function") {
        return ident(rest.trim_start_matches('*').trim_start()).map(Found::Item);
    }
    for keyword in ["class", "interface", "namespace"] {
        if let Some(rest) = after(line, keyword) {
            return ident(rest).map(Found::Container);
        }
    }
    for keyword in ["type", "enum"] {
        if let Some(rest) = after(line, keyword) {
            return ident(rest).map(Found::Item);
        }
    }
    for keyword in ["const", "let", "var"] {
        if let Some(rest) = after(line, keyword) {
            let name = ident(rest)?;
            let value = rest.split_once('=')?.1.trim_start();
            let value = value.strip_prefix("async").map_or(value, str::trim_start);
            let is_function = value.starts_with("function") || value.contains("=>") || value.ends_with("=> {");
            return is_function.then_some(Found::Item(name));
        }
    }
    if inside {
        let line = strip_words(line, &["public", "private", "protected", "static", "readonly", "override", "async", "get", "set"]);
        let line = line.trim_start_matches(['*', '#']);
        if line.ends_with('{') || line.ends_with(';') {
            let name = name_before_paren(line)?;
            return (name != "constructor").then_some(Found::Item(name));
        }
    }
    None
}

fn go(line: &str) -> Option<Found> {
    if let Some(rest) = after(line, "func") {
        if let Some(receiver) = rest.strip_prefix('(') {
            let (recv, tail) = receiver.split_once(')')?;
            let owner = recv.split_whitespace().last()?.trim_start_matches('*');
            let owner = owner.split('[').next().unwrap_or(owner);
            return Some(Found::MemberOf(ident(owner)?, ident(tail.trim_start())?));
        }
        return ident(rest).map(Found::Item);
    }
    after(line, "type").and_then(ident).map(Found::Item)
}

fn managed(line: &str, inside: bool) -> Option<Found> {
    if line.starts_with('@') || line.starts_with('[') {
        return None;
    }
    let line = strip_words(
        line,
        &[
            "public",
            "private",
            "protected",
            "internal",
            "static",
            "final",
            "abstract",
            "sealed",
            "open",
            "data",
            "partial",
            "override",
            "virtual",
            "async",
            "suspend",
            "inline",
            "readonly",
            "unsafe",
            "synchronized",
            "native",
            "default",
            "export",
            "companion",
            "inner",
        ],
    );
    for keyword in ["class", "interface", "enum", "record", "object", "struct"] {
        if let Some(rest) = after(line, keyword) {
            let rest = if keyword == "enum" { rest.strip_prefix("class ").unwrap_or(rest) } else { rest };
            return ident(rest).map(Found::Container);
        }
    }
    if let Some(rest) = after(line, "fun") {
        // Kotlin: `fun <T> List<T>.name(` and `fun name(`.
        let head = rest.split_once('(')?.0;
        let name = head.rsplit(['.', ' ', '>']).next()?;
        return ident(name).map(Found::Item);
    }
    if inside && (line.ends_with('{') || line.ends_with(';') || line.ends_with(')')) {
        return name_before_paren(line).map(Found::Item);
    }
    None
}

fn c_family(line: &str, indent: usize, inside: bool) -> Option<Found> {
    if line.starts_with('#') || line.starts_with("typedef") || line.starts_with("using ") || line.starts_with("extern \"") {
        return None;
    }
    let plain = strip_words(line, &["static", "inline", "constexpr", "virtual", "explicit", "friend", "template"]);
    for keyword in ["class", "struct", "union", "enum"] {
        if let Some(rest) = after(plain, keyword) {
            let rest = rest.strip_prefix("class ").unwrap_or(rest);
            // `struct point p = …;` and `struct point *make(void) {` are not definitions of `point`.
            if line.ends_with(';') && !line.contains('{') || line.contains('(') {
                break;
            }
            return ident(rest).map(Found::Container);
        }
    }
    if plain.starts_with("namespace") {
        return None;
    }
    if (indent == 0 || inside) && !line.ends_with(';') || inside && line.ends_with(';') {
        let name = name_before_paren(plain)?;
        return Some(match name.rsplit_once("::") {
            Some((owner, method)) if !owner.is_empty() && !method.is_empty() => Found::MemberOf(owner.to_string(), method.to_string()),
            _ => Found::Item(name),
        });
    }
    None
}

fn indent_of(line: &str) -> usize {
    line.chars().take_while(|c| c.is_whitespace()).map(|c| if c == '\t' { 4 } else { 1 }).sum()
}

fn is_noise(trimmed: &str, lang: Lang) -> bool {
    trimmed.is_empty()
        || trimmed.starts_with("//")
        || trimmed.starts_with("/*")
        || trimmed.starts_with('*')
        || (lang == Lang::Python && trimmed.starts_with('#'))
        || (lang == Lang::Rust && trimmed.starts_with("#["))
}

/// The block the scan is inside of.
struct Open {
    indent: usize,
    /// Index into the symbol list; `None` for a function body or a hidden block.
    container: Option<usize>,
    /// The indentation of the block's direct children, known from its first line.
    child_indent: Option<usize>,
}

fn add_member(symbols: &mut Vec<Symbol>, owner: &str, member: String) {
    let at = match symbols.iter().position(|s| s.name == owner) {
        Some(at) => at,
        None if symbols.len() < MAX_SYMBOLS => {
            symbols.push(Symbol { name: owner.to_string(), members: Vec::new() });
            symbols.len() - 1
        }
        None => return,
    };
    let members = &mut symbols[at].members;
    if members.len() < MAX_MEMBERS && !members.contains(&member) {
        members.push(member);
    }
}

/// The symbols a file defines, in file order. An unknown file type gives none.
pub fn outline(path: &str, text: &str) -> Vec<Symbol> {
    let Some(lang) = lang_of(path) else { return Vec::new() };
    let mut symbols: Vec<Symbol> = Vec::new();
    let mut open: Option<Open> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if is_noise(line, lang) {
            continue;
        }
        let indent = indent_of(raw);
        // A line at or left of the open block's own indentation ends it (a lone closing brace aside).
        if open.as_ref().is_some_and(|o| indent <= o.indent && !line.starts_with(['}', ')', ']', '{'])) {
            open = None;
        }
        let depth = match &mut open {
            Some(o) if indent > o.indent => {
                let child = *o.child_indent.get_or_insert(indent);
                if indent != child {
                    continue;
                }
                o.container
            }
            Some(_) => continue,
            None => None,
        };
        let inside = depth.is_some();
        if open.is_some() && !inside {
            continue;
        }
        let found = match lang {
            Lang::Python => python(line),
            Lang::Rust => rust(line),
            Lang::Script => script(line, inside),
            Lang::Go => go(line),
            Lang::Managed => managed(line, inside),
            Lang::C => c_family(line, indent, inside),
        };
        let Some(found) = found else { continue };
        match (found, depth) {
            (Found::MemberOf(owner, member), _) => {
                add_member(&mut symbols, &owner, member);
                open = Some(Open { indent, container: None, child_indent: None });
            }
            (Found::Item(name) | Found::Container(name), Some(owner)) => {
                let owner = symbols[owner].name.clone();
                add_member(&mut symbols, &owner, name);
            }
            (Found::Hidden, Some(_)) => {}
            (Found::Hidden, None) => open = Some(Open { indent, container: None, child_indent: None }),
            (Found::Item(name), None) => {
                if symbols.len() < MAX_SYMBOLS && !symbols.iter().any(|s| s.name == name) {
                    symbols.push(Symbol { name, members: Vec::new() });
                }
                // The body of a function is skipped; a one-line item ends at the next line of the same depth.
                open = Some(Open { indent, container: None, child_indent: None });
            }
            (Found::Container(name), None) => {
                let at = match symbols.iter().position(|s| s.name == name) {
                    Some(at) => Some(at),
                    None if symbols.len() < MAX_SYMBOLS => {
                        symbols.push(Symbol { name, members: Vec::new() });
                        Some(symbols.len() - 1)
                    }
                    None => None,
                };
                open = Some(Open { indent, container: at, child_indent: None });
            }
        }
    }
    symbols
}

/// `add, sub, Stack(push, pop)`.
pub fn render(symbols: &[Symbol]) -> String {
    let parts: Vec<String> = symbols
        .iter()
        .map(|s| if s.members.is_empty() { s.name.clone() } else { format!("{}({})", s.name, s.members.join(", ")) })
        .collect();
    parts.join(", ")
}

fn looks_like_test(path: &str) -> bool {
    let low = path.to_ascii_lowercase();
    let name = low.rsplit('/').next().unwrap_or(&low);
    low.starts_with("tests/")
        || low.contains("/tests/")
        || low.contains("/test/")
        || name.starts_with("test_")
        || name.contains("_test.")
        || name.contains(".test.")
        || name.contains(".spec.")
}

/// Lower sorts first: files the task names, then other code, then tests.
fn rank(path: &str, task_low: &str) -> u8 {
    let name = path.rsplit('/').next().unwrap_or(path).to_ascii_lowercase();
    let stem = name.split('.').next().unwrap_or(&name);
    let named = task_low.contains(&name) || (stem.len() >= 4 && task_low.contains(stem));
    match (named, looks_like_test(path)) {
        (true, _) => 0,
        (false, false) => 1,
        (false, true) => 2,
    }
}

/// One line per file, in the order given: the path, and for as many files as `budget` tokens
/// allow, the symbols defined in it. Files the task names get their symbols first.
pub fn listing(root: &Path, files: &[String], task: &str, budget: u32) -> Vec<String> {
    let task_low = task.to_lowercase();
    let mut order: Vec<usize> = (0..files.len()).collect();
    order.sort_by_key(|&i| (rank(&files[i], &task_low), i));
    let mut lines: Vec<String> = files.to_vec();
    let mut left = budget;
    for i in order {
        let path = &files[i];
        if lang_of(path).is_none() {
            continue;
        }
        let abs = root.join(path);
        if std::fs::metadata(&abs).map_or(true, |m| m.len() > MAX_FILE_BYTES) {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&abs) else { continue };
        let symbols = render(&outline(path, &text));
        if symbols.is_empty() {
            continue;
        }
        let cost = estimate_text(&symbols) + 1;
        if cost > left {
            continue;
        }
        left -= cost;
        lines[i] = format!("{path}: {symbols}");
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::TempDir;

    fn map(path: &str, text: &str) -> String {
        render(&outline(path, text))
    }

    #[test]
    fn python_functions_and_classes_with_their_methods() {
        let text = "import os\n\nLIMIT = 3\n\ndef add(a, b):\n    def helper():\n        pass\n    return a + b\n\nasync def fetch(url):\n    pass\n\n\
                    class Stack:\n    \"\"\"A stack.\"\"\"\n\n    def __init__(self):\n        self.items = []\n\n    def push(self, x):\n        if x:\n            def inner(): pass\n        self.items.append(x)\n\n    async def pop(self):\n        return self.items.pop()\n\ndef last():\n    pass\n";
        assert_eq!(map("calc.py", text), "add, fetch, Stack(push, pop), last");
    }

    #[test]
    fn rust_items_and_impl_blocks() {
        let text = "use std::fmt;\n\npub const LIMIT: usize = 3;\n\n#[derive(Debug)]\npub struct Stack<T> {\n    items: Vec<T>,\n}\n\n\
                    impl<T: Clone> Stack<T> {\n    pub fn new() -> Self {\n        let f = |x| x;\n        Self { items: Vec::new() }\n    }\n\n    pub(crate) async fn push(&mut self, x: T) {}\n}\n\n\
                    impl fmt::Display for Stack<u8> {\n    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n        Ok(())\n    }\n}\n\n\
                    pub trait Shape {\n    fn area(&self) -> f32;\n}\n\npub(crate) fn run() {}\n\nmod inner;\n\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn it_works() {}\n}\n";
        assert_eq!(map("src/stack.rs", text), "LIMIT, Stack(new, push, fmt), Shape(area), run");
    }

    #[test]
    fn script_functions_classes_and_arrow_constants() {
        let text = "import x from 'y';\n\nexport function add(a, b) {\n  const inner = () => 1;\n  return a + b;\n}\n\n\
                    export const sub = (a, b) => a - b;\nconst LIMIT = 3;\n\nexport default class Stack {\n  constructor() {\n    this.items = [];\n  }\n\n  push(x) {\n    if (x) {\n      this.items.push(x);\n    }\n  }\n\n  static async pop() {\n  }\n}\n\n\
                    export interface Shape {\n  area(): number;\n}\n\nexport type Id = string;\n";
        assert_eq!(map("src/calc.ts", text), "add, sub, Stack(push, pop), Shape(area), Id");
    }

    #[test]
    fn go_methods_join_their_type() {
        let text = "package main\n\ntype Stack struct {\n\titems []int\n}\n\nfunc (s *Stack) Push(x int) {\n\ts.items = append(s.items, x)\n}\n\nfunc main() {\n\tfmt.Println(1)\n}\n";
        assert_eq!(map("main.go", text), "Stack(Push), main");
    }

    #[test]
    fn managed_classes_inside_a_namespace() {
        let text = "namespace Shop\n{\n    public class Cart\n    {\n        private List<int> items = new List<int>();\n\n        [Obsolete]\n        public void Add(int x)\n        {\n            if (x > 0)\n            {\n                items.Add(x);\n            }\n        }\n\n        public int Count() => items.Count;\n    }\n}\n";
        assert_eq!(map("Cart.cs", text), "Cart(Add, Count)");
        let java = "package shop;\n\npublic class Cart {\n    private final List<Integer> items = new ArrayList<>();\n\n    @Override\n    public String toString() {\n        return \"cart\";\n    }\n}\n";
        assert_eq!(map("Cart.java", java), "Cart(toString)");
    }

    #[test]
    fn c_functions_and_types() {
        let text = "#include <stdio.h>\n\nstruct point {\n    int x;\n};\n\nstatic int add(int a, int b)\n{\n    return a + b;\n}\n\nint main(void) {\n    printf(\"%d\", add(1, 2));\n    return 0;\n}\n\nint declared(void);\n";
        assert_eq!(map("main.c", text), "point, add, main");
        assert_eq!(map("stack.cpp", "void Stack::push(int x) {\n    items.push_back(x);\n}\n"), "Stack(push)");
    }

    #[test]
    fn other_files_have_no_outline_and_limits_hold() {
        assert_eq!(map("README.md", "# def not_code():\n"), "");
        let many: String = (0..80).map(|i| format!("def f{i}():\n    pass\n")).collect();
        assert_eq!(outline("big.py", &many).len(), MAX_SYMBOLS);
        let wide: String = "class C:\n".to_string() + &(0..40).map(|i| format!("    def m{i}(self):\n        pass\n")).collect::<String>();
        assert_eq!(outline("wide.py", &wide)[0].members.len(), MAX_MEMBERS);
    }

    #[test]
    fn the_listing_spends_its_budget_on_the_files_the_task_names() {
        let t = TempDir::new("map");
        t.write("alpha.py", "def one():\n    pass\n").write("calc.py", "def add(a, b):\n    return a - b\n").write("README.md", "# x\n");
        t.write("tests/test_calc.py", "def test_add():\n    pass\n");
        let files: Vec<String> = ["README.md", "alpha.py", "calc.py", "tests/test_calc.py"].map(String::from).to_vec();
        let all = listing(t.path(), &files, "Fix add in calc.py", 500);
        assert_eq!(all, ["README.md", "alpha.py: one", "calc.py: add", "tests/test_calc.py: test_add"]);
        // Room for one outline only: the named file gets it.
        let one = listing(t.path(), &files, "Fix add in calc.py", 2);
        assert_eq!(one, ["README.md", "alpha.py", "calc.py: add", "tests/test_calc.py"]);
        assert_eq!(listing(t.path(), &files, "Fix it", 0), files);
    }
}
