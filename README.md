# CAP — Code As Planned

A coding agent for small local models, written in Rust.

**The model proposes; the code decides.** The model suggests the next step. Whether a file may be changed, whether a command may run, and whether the work is finished are decided by the harness. A run ends as *done* only when the project's tests pass.

![cap exec fixes a one-line bug: it reads the file, runs the tests, replaces one line, and the tests pass](docs/demo.gif)

<sub>One real run on Linux with `qwen3.5-9b` on the KAU server, commands isolated by Landlock. Pauses longer than 3 s are cut to 3 s; the run took 22.6 s.</sub>

Status: **alpha** (`0.15.0-alpha.1`). The conversation (`cap`), the one-shot command (`cap exec`), isolation of commands by the operating system, skills, MCP servers and the settings file work and are covered by tests. How much each part helps a real model is not measured yet. The plan is in [docs/PLAN.md](docs/PLAN.md); the principles are in [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) (both in Korean).

## Build

Rust 1.88 or newer.

```sh
git clone https://github.com/juyoung020/cap-agent
cd cap-agent
cargo build --release
```

The result is one executable: `target/release/cap` (`cap.exe` on Windows).

## Use

### A conversation

```sh
cap -C path/to/project
```

`cap` alone starts a conversation in the project folder. Type a task or a question; the model's text is shown while it arrives, and each turn ends with the verdict, the changed files, the steps, the time and the context in use.

| Command | Meaning |
|---|---|
| `/help` | The list of commands |
| `/status` | The model, the context in use, the mode, the tool set, the tests and the isolation |
| `/mode NAME` | `read`, `edit` or `auto` |
| `/model NAME` | Talk to another model of the same server |
| `/clear` | Start over with an empty conversation |
| `/skills` | The skills the agent can use here |
| `/mcp` | The outside tool servers and their tools |
| `/quit` | End (so does the end of the input) |

A line that ends with `\` continues in the next line. Ctrl-C stops the turn that is running.

### One task

```sh
cap exec "median() gives the wrong value for a list with an even number of items. Fix it." -C path/to/project
```

CAP reads the project, edits files, runs the tests and answers. The answer goes to stdout; progress goes to stderr. This is the screen of one run with `qwen3.5-9b`:

```
cap 0.15.0-alpha.1 · kau qwen3.5-9b · 16384 tokens · mode edit · tools small
folder path/to/project · tests: python -m pytest -q -p no:cacheprovider · isolation: off
> read_file {"path": "stats.py"}
  [ok] stats.py lines 1-10 of 10
> run_tests {}
= tests fail: 1 failed, 1 passed in 0.04s
  [ok] run_tests: tests fail
  The issue is in the median function. When the list has an even number of items, the indices are off by one. For a list of length 4, the mid index is 2 (0-indexe…
> replace_lines {"path": "stats.py", "start": 10, "end": 10, "new": "return (ordered[mid - 1] + ordered[mid]) / 2"}
  [ok] stats.py: lines 10-10 replaced (1 -> 1 lines) (`new` had no indentation; it got that of the replaced lines). Now:
= tests pass

Fixed the median function by changing the indices from `mid` and `mid + 1` to `mid - 1` and `mid` for even-length lists.
DONE — the tests pass
changed: stats.py
4 steps · 25.1 s · 9482 tokens in, 280 out · 0 rejected "done"
```

### Options

`cap` and `cap exec` share these options.

| Option | Meaning |
|---|---|
| `-C, --dir <DIR>` | The project folder (default: the current folder) |
| `--test <CMD>` | The command that decides whether the work is done |
| `--no-test` | Run without a test command |
| `--mode <read\|edit\|auto>` | What the agent may do (default `edit`) |
| `--max-steps <N>` | Model calls before a turn stops (default 30) |
| `--provider <NAME>` | A name from `cap providers` (default `ollama`, env `CAP_PROVIDER`) |
| `--model <NAME>` | The model name on that server (env `CAP_MODEL`) |
| `--base-url <URL>` | Any OpenAI-compatible endpoint (env `CAP_BASE_URL`) |
| `--context <TOKENS>` | The model's context window (env `CAP_CONTEXT`) |
| `--no-stream` | Show the model's text when a reply is complete |
| `--strict-tools` | Make every reply a tool call, for servers that can require one |
| `--tools <small\|standard\|full>` | The tool set (default: picked from the context window) |
| `--sandbox <auto\|on\|off>` | Isolation for commands (default `auto`) |

`cap exec` adds:

| Option | Meaning |
|---|---|
| `--json` | Print one JSON object with the result |
| `--trace <FILE>` | Write the whole conversation as JSON lines |

A flag wins over an environment variable, which wins over the settings file.

Without `--test`, CAP finds the test command from the project files: `cargo test` for `Cargo.toml`, `pytest` for Python projects, `npm test` for a `package.json` with a test script, `go test ./...` for `go.mod`.

### Result and exit code

| Headline | Status | Exit code of `cap exec` |
|---|---|---|
| `DONE — the tests pass` | `verified` | 0 |
| `ANSWERED` (a question, nothing changed) | `answered` | 0 |
| `DONE, NOT VERIFIED` (files changed, the project has no tests) | `unverified` | 3 |
| `NOT DONE` | `tests_fail`, `step_limit`, `stuck`, `model_failed`, `context_full` | 1 |
| `STOPPED` (Ctrl-C) | `interrupted` | 130 |
| a setup or usage error | | 2 |

When the model says it is finished, CAP runs the tests. If they fail, the failure output goes back to the model and the work continues. After four rejected "done" claims, or the same failure three times, the run ends as `tests_fail`.

### Modes

| Mode | Files | Commands |
|---|---|---|
| `read` | read only | read-only commands run; others ask first |
| `edit` | edit inside the project | read-only commands and test runners run; others ask first |
| `auto` | edit inside the project | ordinary commands run; network, package and git-history commands ask first |

Fixed rules in every mode:

- Paths outside the project are refused.
- Files that hold keys (`.env`, `*.pem`, `.ssh/`, …) are refused for reading and writing, also when a command names them.
- Destructive commands (`rm -rf`, `git reset --hard`, a forced push, …) are refused.
- Changing an existing test file asks first.
- Without a terminal to ask on, the answer to every question is no.

## What the harness does for the model

- **Edits by line number.** `replace_lines` takes a line range and the new lines, so the model writes only what changes. The call is applied only to lines the model has seen in their current state; for other lines the answer shows them, and the same call works when it is sent again. New lines that come without indentation get the indentation of the lines they replace.
- **A tool set that fits the model.** Up to 16 384 tokens of context the model gets six tools with short descriptions (`small`); up to 64 000 it also gets `list_files` and `edit_file` (`standard`); above that also `note` (`full`).
- **A code map.** The first prompt lists the project files with the functions and types each one defines (Python, Rust, JavaScript and TypeScript, Go, Java, Kotlin, C#, C and C++), and the project's `AGENTS.md` or `CAP.md`.
- **A work state board.** The last lines of every request say the step, the files changed and read, the state of the tests and the next step when it is clear. The stored conversation stays as it was, so a server that caches prompt prefixes can reuse it.
- **Work ahead of the model.** Files named in the task are in the first prompt. After a change the tests start on their own: the result of a fast suite arrives with the tool result, and a slow suite keeps running while the model writes.
- **Help with common slips.** Tool and argument names of other agents are accepted, broken arguments are explained, a call that failed twice the same way is cut short, and tool calls written out as text are read as calls.

## Isolation

Commands the agent runs are kept inside the project by the operating system.

| System | Mechanism | A command can |
|---|---|---|
| Linux 5.13 or newer | Landlock | read the system and the toolchains; write to the project, the temp folders and the build caches |
| Windows | AppContainer | open the folders handed to it; it has no network |

On Linux this needs no set-up. On Windows a folder is handed to the container once, which changes the folder's access list:

```sh
cap sandbox setup -C path/to/project --tool C:\path\to\python
cap sandbox status -C path/to/project
cap sandbox undo -C path/to/project
```

`--sandbox auto` isolates where it can and says in the header where it cannot (`isolation: off`). `--sandbox on` starts only with isolation. `--sandbox off` runs commands as your user with the permission table as the guard.

## Settings file

`cap config` shows the settings in force and where the file lives: `%APPDATA%\cap\config.toml` on Windows, `~/.config/cap/config.toml` elsewhere, or the path in `CAP_CONFIG`.

```toml
provider = "local"
mode = "edit"
max_steps = 30
stream = true
eager_tests = true      # start the tests after each change
strict_tools = false
tools = "small"

[sandbox]
mode = "auto"
read = ["/opt/toolchain"]
write = ["/var/cache/build"]

[providers.local]
base_url = "http://localhost:8000/v1"
model = "my-model"
context = 16384
key_env = "LOCAL_API_KEY"

[mcp.docs]
command = "docs-server"
args = ["--stdio"]
allow = true            # its tools run without asking
```

## Skills and MCP servers

- **Skills.** A folder with a `SKILL.md` under `.cap/skills`, `.claude/skills` or `.agents/skills`, in the project or in your home folder, is a skill. The prompt carries its name and one line of description; the model reads the body with `use_skill` when it needs it. `cap skills` lists them.
- **MCP servers.** Servers that speak MCP over stdin and stdout are set under `[mcp.NAME]` in the settings file. Their tools are offered as `mcp__<server>__<tool>`. A call asks first unless the mode is `auto` or the server has `allow = true`. `cap mcp` starts the servers and lists their tools.

## Model servers

```
$ cap providers
name        model           context  key                 endpoint
ollama      qwen3:8b           8192  none needed         http://localhost:11434/v1
lmstudio    --model            8192  none needed         http://localhost:1234/v1
kau         qwen3.5-9b        16384  KAU_API_KEY         https://agent.kau.ac.kr/v1
openai      --model          128000  OPENAI_API_KEY      https://api.openai.com/v1
anthropic   --model          200000  ANTHROPIC_API_KEY   https://api.anthropic.com/v1
gemini      --model          128000  GEMINI_API_KEY      https://generativelanguage.googleapis.com/v1beta/openai
openrouter  --model          128000  OPENROUTER_API_KEY  https://openrouter.ai/api/v1
```

The key is read from the provider's environment variable or from `CAP_API_KEY`. The list also says whether each key is set.

```sh
cap exec "add a --verbose flag" --provider ollama --model qwen3:8b
cap exec "why does test_login fail?" --mode read --provider openai --model <model>
cap exec "fix the failing test" --base-url http://localhost:8000/v1 --model my-model --context 16384
```

**Context on local servers.** Ollama and LM Studio load a model with a small context window unless told otherwise. CAP assumes 8192 tokens for them, so set the server to at least that (`OLLAMA_CONTEXT_LENGTH` or `num_ctx` for Ollama, the context length setting in LM Studio). When you set a larger window, pass the same number with `--context`.

## What is tested

- 153 tests on Windows and 152 on Linux (`cargo test --workspace`): the tools, line edits, the permission table, the test verdicts, context fitting, the code map, the board, skills, the MCP protocol, the settings file, the loop and whole conversations against a scripted model, the HTTP client against a local mock server, and the isolation against real commands (Landlock on Linux, AppContainer on Windows).
- Single trial runs with `qwen3.5-9b` on the KAU server: the one-line bug fix above on Windows with isolation off (4 model calls; 17.9 s and 25.1 s in two runs), the same fix on Linux with Landlock isolation (4 model calls, 22.6 s; the recording at the top), and a piped conversation with one question and the slash commands.
- The other provider entries are checked against the mock server only. MCP is checked with the protocol tests; no outside MCP server has been run with it.
- The effect of each feature on a real model has not been measured. A comparison with other agents on the same model and tasks is phase 6 of the plan and has not been run.

## Layout

```
crates/cap-core   the agent as a library: loop, tools, edits, test gate, permissions, context,
                  board, code map, skills, MCP client, isolation, model client
crates/cap        the command line program: conversation, exec, settings file
docs/             plan and principles
archive/python    the Python version (0.8.0 – 0.14.0)
```

## The Python version

CAP 0.14.0 and earlier were written in Python. That version stays installable from its tag, and its sources are under [archive/python](archive/python):

```sh
uv tool install git+https://github.com/juyoung020/cap-agent@v0.14.0
```

## License

Apache-2.0. See [LICENSE](LICENSE).
