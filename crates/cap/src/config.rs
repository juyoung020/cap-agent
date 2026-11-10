//! The settings of a run. A flag wins over an environment variable, which wins over the user's
//! settings file, which wins over what is built in.

use cap_core::mcp::ServerConfig;
use cap_core::perm::Mode;
use cap_core::sandbox::Want;
use cap_core::tools::Profile;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;

/// Reads an environment variable; tests pass their own.
pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

/// The real environment: a variable that is empty counts as not set.
pub fn process_env(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

/// A model server CAP knows by name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Provider {
    pub name: String,
    pub base_url: String,
    /// The environment variable that holds the key; empty for servers that need none.
    pub key_env: String,
    /// The model used when none is named; empty when the person has to name one.
    pub model: String,
    pub context: u32,
    /// The server needs to be told to skip thinking output.
    pub no_thinking: bool,
}

/// The window assumed for a server the settings file adds without saying how large it is.
const UNKNOWN_CONTEXT: u32 = 8192;

pub fn builtins() -> Vec<Provider> {
    let p = |name: &str, base_url: &str, key_env: &str, model: &str, context: u32, no_thinking: bool| Provider {
        name: name.to_string(),
        base_url: base_url.to_string(),
        key_env: key_env.to_string(),
        model: model.to_string(),
        context,
        no_thinking,
    };
    vec![
        p("ollama", "http://localhost:11434/v1", "", "qwen3:8b", 8192, false),
        p("lmstudio", "http://localhost:1234/v1", "", "", 8192, false),
        p("kau", "https://agent.kau.ac.kr/v1", "KAU_API_KEY", "qwen3.5-9b", 16_384, true),
        p("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "", 128_000, false),
        p("anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", "", 200_000, false),
        p("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "", 128_000, false),
        p("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "", 128_000, false),
    ]
}

/// The settings file as written.
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct File {
    pub provider: Option<String>,
    pub mode: Option<String>,
    pub max_steps: Option<u32>,
    pub stream: Option<bool>,
    pub eager_tests: Option<bool>,
    pub strict_tools: Option<bool>,
    pub tools: Option<String>,
    #[serde(default)]
    pub sandbox: SandboxFile,
    #[serde(default)]
    pub providers: BTreeMap<String, ProviderFile>,
    #[serde(default)]
    pub mcp: BTreeMap<String, McpFile>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SandboxFile {
    pub mode: Option<String>,
    /// More folders the commands may read.
    #[serde(default)]
    pub read: Vec<PathBuf>,
    /// More folders the commands may read and write.
    #[serde(default)]
    pub write: Vec<PathBuf>,
}

/// Changes to a built-in server, or a new one (which needs `base_url`).
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProviderFile {
    pub base_url: Option<String>,
    pub key_env: Option<String>,
    pub model: Option<String>,
    pub context: Option<u32>,
    pub no_thinking: Option<bool>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct McpFile {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Calls to this server run without asking.
    #[serde(default)]
    pub allow: bool,
}

/// The person's home folder.
pub fn home(env: Env) -> Option<PathBuf> {
    env(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

/// Where the settings file lives: `CAP_CONFIG`, or the place this system keeps settings.
pub fn path(env: Env) -> Option<PathBuf> {
    if let Some(named) = env("CAP_CONFIG") {
        return Some(PathBuf::from(named));
    }
    let base = if cfg!(windows) {
        env("APPDATA").map(PathBuf::from)
    } else {
        env("XDG_CONFIG_HOME").map(PathBuf::from).or_else(|| env("HOME").map(|h| PathBuf::from(h).join(".config")))
    };
    base.map(|b| b.join("cap").join("config.toml"))
}

pub fn parse(text: &str) -> Result<File, String> {
    toml::from_str(text).map_err(|e| e.to_string().trim_end().to_string())
}

/// The settings file and whether it exists.
pub struct Loaded {
    pub path: Option<PathBuf>,
    pub found: bool,
    pub file: File,
}

/// Read the settings file. A file that is not there means built-in settings, except when
/// `CAP_CONFIG` names it.
pub fn load(env: Env) -> Result<Loaded, String> {
    let Some(path) = path(env) else {
        return Ok(Loaded { path: None, found: false, file: File::default() });
    };
    match std::fs::read_to_string(&path) {
        Ok(text) => {
            let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
            let file = parse(text).map_err(|e| format!("{}: {e}", path.display()))?;
            Ok(Loaded { path: Some(path), found: true, file })
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && env("CAP_CONFIG").is_none() => {
            Ok(Loaded { path: Some(path), found: false, file: File::default() })
        }
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// What the command line said.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Flags {
    pub provider: Option<String>,
    pub model: Option<String>,
    pub base_url: Option<String>,
    pub context: Option<u32>,
    pub mode: Option<String>,
    pub max_steps: Option<u32>,
    pub no_stream: bool,
    pub strict_tools: bool,
    pub tools: Option<String>,
    pub sandbox: Option<String>,
}

/// Everything a run needs to know, decided.
#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    /// The server in use, with the model, address and window of this run filled in.
    pub provider: Provider,
    pub mode: Mode,
    pub max_steps: u32,
    pub stream: bool,
    pub eager_tests: bool,
    pub strict_tools: bool,
    /// The tool set; `None` picks one from the size of the context window.
    pub profile: Option<Profile>,
    pub sandbox: Want,
    pub sandbox_read: Vec<PathBuf>,
    pub sandbox_write: Vec<PathBuf>,
    pub mcp: Vec<ServerConfig>,
}

/// The built-in servers with the file's changes and additions.
pub fn providers(file: &File) -> Result<Vec<Provider>, String> {
    let mut all = builtins();
    for (name, change) in &file.providers {
        let name = name.trim().to_lowercase();
        if name.is_empty() {
            return Err("a provider in the settings file has no name".to_string());
        }
        let at = match all.iter().position(|p| p.name == name) {
            Some(at) => at,
            None => {
                let Some(base_url) = change.base_url.clone() else {
                    return Err(format!("[providers.{name}] is a new server and needs base_url"));
                };
                all.push(Provider {
                    name,
                    base_url,
                    key_env: String::new(),
                    model: String::new(),
                    context: UNKNOWN_CONTEXT,
                    no_thinking: false,
                });
                all.len() - 1
            }
        };
        let p = &mut all[at];
        if let Some(v) = &change.base_url {
            p.base_url = v.clone();
        }
        if let Some(v) = &change.key_env {
            p.key_env = v.trim().to_string();
        }
        if let Some(v) = &change.model {
            p.model = v.trim().to_string();
        }
        if let Some(v) = change.context {
            p.context = v;
        }
        if let Some(v) = change.no_thinking {
            p.no_thinking = v;
        }
    }
    Ok(all)
}

pub fn parse_mode(text: &str) -> Result<Mode, String> {
    Mode::parse(&text.trim().to_lowercase()).ok_or_else(|| format!("the mode is read, edit or auto (got {text})"))
}

pub fn resolve(flags: &Flags, env: Env, file: &File) -> Result<Settings, String> {
    let all = providers(file)?;
    let wanted = flags.provider.clone().or_else(|| env("CAP_PROVIDER")).or_else(|| file.provider.clone());
    let wanted = wanted.map_or_else(|| "ollama".to_string(), |n| n.trim().to_lowercase());
    let Some(mut provider) = all.iter().find(|p| p.name == wanted).cloned() else {
        let names: Vec<&str> = all.iter().map(|p| p.name.as_str()).collect();
        return Err(format!("no provider named {wanted}. The names are: {}", names.join(", ")));
    };
    if let Some(model) = flags.model.clone().or_else(|| env("CAP_MODEL")) {
        provider.model = model.trim().to_string();
    }
    if let Some(url) = flags.base_url.clone().or_else(|| env("CAP_BASE_URL")) {
        provider.base_url = url.trim().to_string();
    }
    let context = match (flags.context, env("CAP_CONTEXT")) {
        (Some(n), _) => Some(n),
        (None, Some(text)) => Some(text.parse::<u32>().map_err(|_| format!("CAP_CONTEXT is a number of tokens (got {text})"))?),
        (None, None) => None,
    };
    if let Some(n) = context {
        provider.context = n;
    }
    if provider.context == 0 {
        return Err("the context window is a number of tokens above 0".to_string());
    }

    let mode = match flags.mode.as_deref().or(file.mode.as_deref()) {
        Some(text) => parse_mode(text)?,
        None => Mode::Edit,
    };
    let max_steps = flags.max_steps.or(file.max_steps).unwrap_or(30);
    if max_steps == 0 {
        return Err("max_steps is at least 1".to_string());
    }
    let profile = match flags.tools.as_deref().or(file.tools.as_deref()) {
        Some(text) => Some(Profile::parse(text).ok_or_else(|| format!("the tool set is small, standard or full (got {text})"))?),
        None => None,
    };
    let sandbox = match flags.sandbox.as_deref().or(file.sandbox.mode.as_deref()) {
        Some(text) => Want::parse(text).ok_or_else(|| format!("the isolation setting is auto, on or off (got {text})"))?,
        None => Want::Auto,
    };
    let mut mcp = Vec::new();
    for (name, server) in &file.mcp {
        if server.command.trim().is_empty() {
            return Err(format!("[mcp.{name}] needs a command"));
        }
        mcp.push(ServerConfig {
            name: name.clone(),
            command: server.command.clone(),
            args: server.args.clone(),
            env: server.env.clone(),
            allow: server.allow,
        });
    }
    Ok(Settings {
        provider,
        mode,
        max_steps,
        stream: !flags.no_stream && file.stream.unwrap_or(true),
        eager_tests: file.eager_tests.unwrap_or(true),
        strict_tools: flags.strict_tools || file.strict_tools.unwrap_or(false),
        profile,
        sandbox,
        sandbox_read: file.sandbox.read.clone(),
        sandbox_write: file.sandbox.write.clone(),
        mcp,
    })
}

/// The key for a server: `CAP_API_KEY`, or the variable the server names.
pub fn key(provider: &Provider, env: Env) -> Result<String, String> {
    let own = if provider.key_env.is_empty() { None } else { env(&provider.key_env) };
    match env("CAP_API_KEY").or(own) {
        Some(key) => Ok(key),
        None if provider.key_env.is_empty() => Ok("local".to_string()),
        None => Err(format!("{} needs a key: set {} or CAP_API_KEY", provider.name, provider.key_env)),
    }
}

/// Where the key comes from, without the key.
pub fn key_source(provider: &Provider, env: Env) -> String {
    if env("CAP_API_KEY").is_some() {
        return "CAP_API_KEY (set)".to_string();
    }
    match provider.key_env.as_str() {
        "" => "none needed".to_string(),
        name if env(name).is_some() => format!("{name} (set)"),
        name => format!("{name} (not set)"),
    }
}

/// The settings in force, for `cap config`.
pub fn describe(loaded: &Loaded, s: &Settings, env: Env) -> String {
    let p = &s.provider;
    let file = match &loaded.path {
        Some(path) if loaded.found => path.display().to_string(),
        Some(path) => format!("{} (not there: built-in settings)", path.display()),
        None => "none (no settings folder on this system)".to_string(),
    };
    let folders = |list: &[PathBuf]| list.iter().map(|d| d.display().to_string()).collect::<Vec<_>>().join(", ");
    let mut out = vec![
        format!("settings file  {file}"),
        format!("provider       {}", p.name),
        format!("endpoint       {}", p.base_url),
        format!("model          {}", if p.model.is_empty() { "(name one with --model)" } else { &p.model }),
        format!("context        {} tokens", p.context),
        format!("key            {}", key_source(p, env)),
        format!("mode           {}", s.mode.as_str()),
        format!("max_steps      {}", s.max_steps),
        format!("stream         {}", s.stream),
        format!("eager_tests    {}", s.eager_tests),
        format!("strict_tools   {}", s.strict_tools),
        format!("tools          {}", s.profile.unwrap_or_else(|| Profile::for_window(p.context)).as_str()),
        format!("sandbox        {}", s.sandbox.as_str()),
    ];
    if !s.sandbox_read.is_empty() {
        out.push(format!("  also read    {}", folders(&s.sandbox_read)));
    }
    if !s.sandbox_write.is_empty() {
        out.push(format!("  also write   {}", folders(&s.sandbox_write)));
    }
    for server in &s.mcp {
        let how = if server.allow { "runs without asking" } else { "asks before each call" };
        out.push(format!("mcp.{:<10} {} {} ({how})", server.name, server.command, server.args.join(" ")));
    }
    out.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn file(text: &str) -> File {
        parse(text).unwrap()
    }

    #[test]
    fn built_in_settings_start_small_and_local() {
        let s = resolve(&Flags::default(), &no_env, &File::default()).unwrap();
        assert_eq!((s.provider.name.as_str(), s.provider.model.as_str(), s.provider.context), ("ollama", "qwen3:8b", 8192));
        assert_eq!((s.mode, s.max_steps, s.stream, s.eager_tests, s.strict_tools), (Mode::Edit, 30, true, true, false));
        assert_eq!((s.profile, s.sandbox), (None, Want::Auto));
        assert!(s.mcp.is_empty());
        assert_eq!(key(&s.provider, &no_env).unwrap(), "local");
    }

    #[test]
    fn a_flag_wins_over_the_environment_and_the_environment_over_the_file() {
        let f = file("provider = \"kau\"\nmode = \"read\"\nmax_steps = 12\nstream = false\n");
        let from_file = resolve(&Flags::default(), &no_env, &f).unwrap();
        assert_eq!(
            (from_file.provider.name.as_str(), from_file.mode, from_file.max_steps, from_file.stream),
            ("kau", Mode::Read, 12, false)
        );
        assert!(from_file.provider.no_thinking);

        let env = |name: &str| match name {
            "CAP_PROVIDER" => Some("openai".to_string()),
            "CAP_MODEL" => Some("m-env".to_string()),
            "CAP_CONTEXT" => Some("32000".to_string()),
            _ => None,
        };
        let from_env = resolve(&Flags::default(), &env, &f).unwrap();
        assert_eq!(
            (from_env.provider.name.as_str(), from_env.provider.model.as_str(), from_env.provider.context),
            ("openai", "m-env", 32_000)
        );

        let flags = Flags {
            provider: Some("LMStudio".into()),
            model: Some("m-flag".into()),
            context: Some(4096),
            mode: Some("Auto".into()),
            max_steps: Some(5),
            ..Flags::default()
        };
        let from_flags = resolve(&flags, &env, &f).unwrap();
        assert_eq!(from_flags.provider.name, "lmstudio");
        assert_eq!((from_flags.provider.model.as_str(), from_flags.provider.context), ("m-flag", 4096));
        assert_eq!((from_flags.mode, from_flags.max_steps), (Mode::Auto, 5));
    }

    #[test]
    fn the_file_changes_a_server_and_adds_one() {
        let f = file(
            "provider = \"box\"\n\n[providers.kau]\nmodel = \"other\"\ncontext = 32768\n\n[providers.box]\nbase_url = \"http://10.0.0.2:8000/v1\"\nkey_env = \"BOX_KEY\"\nmodel = \"tiny\"\n",
        );
        let all = providers(&f).unwrap();
        let kau = all.iter().find(|p| p.name == "kau").unwrap();
        assert_eq!((kau.model.as_str(), kau.context, kau.base_url.as_str()), ("other", 32_768, "https://agent.kau.ac.kr/v1"));
        let s = resolve(&Flags::default(), &no_env, &f).unwrap();
        assert_eq!((s.provider.name.as_str(), s.provider.model.as_str(), s.provider.context), ("box", "tiny", UNKNOWN_CONTEXT));

        assert!(key(&s.provider, &no_env).unwrap_err().contains("BOX_KEY"));
        let with_key = |name: &str| (name == "BOX_KEY").then(|| "k".to_string());
        assert_eq!(key(&s.provider, &with_key).unwrap(), "k");
        assert_eq!(key_source(&s.provider, &with_key), "BOX_KEY (set)");
        assert_eq!(key_source(&s.provider, &no_env), "BOX_KEY (not set)");

        let new_without_address = file("[providers.box]\nmodel = \"tiny\"\n");
        assert!(providers(&new_without_address).unwrap_err().contains("base_url"));
    }

    #[test]
    fn isolation_tools_and_outside_servers_come_from_the_file() {
        let f = file(
            "tools = \"full\"\nstrict_tools = true\neager_tests = false\n\n[sandbox]\nmode = \"off\"\nread = [\"/opt/sdk\"]\nwrite = [\"/var/build\"]\n\n[mcp.files]\ncommand = \"npx\"\nargs = [\"-y\", \"server\"]\nallow = true\n\n[mcp.files.env]\nTOKEN = \"x\"\n\n[mcp.db]\ncommand = \"db-mcp\"\n",
        );
        let s = resolve(&Flags::default(), &no_env, &f).unwrap();
        assert_eq!((s.profile, s.strict_tools, s.eager_tests, s.sandbox), (Some(Profile::Full), true, false, Want::Off));
        assert_eq!(s.sandbox_read, [PathBuf::from("/opt/sdk")]);
        assert_eq!(s.sandbox_write, [PathBuf::from("/var/build")]);
        assert_eq!(s.mcp.len(), 2);
        assert_eq!((s.mcp[0].name.as_str(), s.mcp[0].command.as_str(), s.mcp[0].allow), ("db", "db-mcp", false));
        assert_eq!((s.mcp[1].name.as_str(), s.mcp[1].args.len(), s.mcp[1].allow), ("files", 2, true));
        assert_eq!(s.mcp[1].env.get("TOKEN").map(String::as_str), Some("x"));

        let flags = Flags { sandbox: Some("on".into()), tools: Some("small".into()), no_stream: true, ..Flags::default() };
        let s = resolve(&flags, &no_env, &f).unwrap();
        assert_eq!((s.sandbox, s.profile, s.stream), (Want::On, Some(Profile::Small), false));
    }

    #[test]
    fn a_wrong_setting_is_named() {
        assert!(parse("modle = \"edit\"\n").unwrap_err().contains("modle"));
        assert!(parse("[mcp.x]\nargs = []\n").unwrap_err().contains("command"));
        let bad = |flags: Flags| resolve(&flags, &no_env, &File::default()).unwrap_err();
        assert!(bad(Flags { provider: Some("nobody".into()), ..Flags::default() }).contains("ollama, lmstudio, kau"));
        assert!(bad(Flags { mode: Some("write".into()), ..Flags::default() }).contains("read, edit or auto"));
        assert!(bad(Flags { tools: Some("huge".into()), ..Flags::default() }).contains("small, standard or full"));
        assert!(bad(Flags { sandbox: Some("maybe".into()), ..Flags::default() }).contains("auto, on or off"));
        assert!(bad(Flags { max_steps: Some(0), ..Flags::default() }).contains("at least 1"));
        let env = |name: &str| (name == "CAP_CONTEXT").then(|| "big".to_string());
        assert!(resolve(&Flags::default(), &env, &File::default()).unwrap_err().contains("CAP_CONTEXT"));
        assert!(resolve(&Flags::default(), &no_env, &file("[mcp.x]\ncommand = \" \"\n")).unwrap_err().contains("[mcp.x]"));
    }

    #[test]
    fn the_file_is_found_where_the_system_keeps_settings() {
        let named = |name: &str| (name == "CAP_CONFIG").then(|| "x/cap.toml".to_string());
        assert_eq!(path(&named), Some(PathBuf::from("x/cap.toml")));
        let system = |name: &str| match name {
            "APPDATA" => Some("A".to_string()),
            "HOME" => Some("H".to_string()),
            _ => None,
        };
        let expect = if cfg!(windows) { PathBuf::from("A").join("cap") } else { PathBuf::from("H").join(".config").join("cap") };
        assert_eq!(path(&system), Some(expect.join("config.toml")));
        assert_eq!(path(&no_env), None);

        // A file that is not there means built-in settings; a file `CAP_CONFIG` names has to exist.
        let missing = |name: &str| match name {
            "APPDATA" | "HOME" => Some("no-such-folder-for-cap".to_string()),
            _ => None,
        };
        let loaded = load(&missing).unwrap();
        assert!(!loaded.found && loaded.file == File::default());
        let named_missing = |name: &str| (name == "CAP_CONFIG").then(|| "no-such-folder-for-cap/cap.toml".to_string());
        assert!(load(&named_missing).is_err());
    }

    #[test]
    fn the_description_shows_where_the_key_comes_from_and_never_the_key() {
        let f = file("provider = \"kau\"\n\n[mcp.files]\ncommand = \"npx\"\nargs = [\"server\"]\n");
        let env = |name: &str| (name == "KAU_API_KEY").then(|| "sk-very-secret".to_string());
        let s = resolve(&Flags::default(), &env, &f).unwrap();
        let loaded = Loaded { path: Some(PathBuf::from("c.toml")), found: true, file: f };
        let text = describe(&loaded, &s, &env);
        assert!(text.contains("KAU_API_KEY (set)") && !text.contains("sk-very-secret"), "{text}");
        assert!(text.contains("qwen3.5-9b") && text.contains("16384 tokens") && text.contains("tools          small"), "{text}");
        assert!(text.contains("mcp.files") && text.contains("asks before each call"), "{text}");
    }
}
