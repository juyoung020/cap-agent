# CAP — Code As Planned

A coding agent for small local models, written in Rust.

**The model proposes; the code decides.** The model suggests the next step. Whether a file may be changed, whether a command may run, and whether the work is finished are decided by the harness. A run ends as *done* only when the project's tests pass.

Status: **alpha** (`0.15.0-alpha.1`). The core loop and the one-shot command `cap exec` work. The plan and what comes next are in [docs/PLAN.md](docs/PLAN.md); the principles are in [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) (both in Korean).

## Build

Rust 1.88 or newer.

```sh
git clone https://github.com/juyoung020/cap-agent
cd cap-agent
cargo build --release
```

The result is one executable: `target/release/cap` (`cap.exe` on Windows).

## Use

```sh
cap exec "inventory.remove() does not reject amounts above the stock. Fix it." -C path/to/project
```

CAP reads the project, edits files, runs the tests and answers. The answer goes to stdout; progress goes to stderr, one short line per call and result, in this shape:

```
> read_file {"path": "inventory.py"}
  [ok] inventory.py lines 1-24 of 24
> edit_file {"path": "inventory.py", "old": "…", "new": "…"}
  [ok] inventory.py: lines 14-15 replaced (2 -> 4 lines)
> run_tests {}
= tests pass

DONE — the tests pass
changed: inventory.py
4 steps · 19.4 s · 5145 tokens in, 236 out · 0 rejected "done"
```

### Options of `cap exec`

| Option | Meaning |
|---|---|
| `-C, --dir <DIR>` | The project folder (default: the current folder) |
| `--test <CMD>` | The command that decides whether the work is done |
| `--no-test` | Run without a test command |
| `--mode <read\|edit\|auto>` | What the agent may do (default `edit`) |
| `--max-steps <N>` | Model calls before the run stops (default 30) |
| `--provider <NAME>` | A name from `cap providers` (default `ollama`, env `CAP_PROVIDER`) |
| `--model <NAME>` | The model name on that server (env `CAP_MODEL`) |
| `--base-url <URL>` | Any OpenAI-compatible endpoint (env `CAP_BASE_URL`) |
| `--context <TOKENS>` | The model's context window (env `CAP_CONTEXT`) |
| `--json` | Print one JSON object with the result |
| `--trace <FILE>` | Write the whole conversation as JSON lines |

Without `--test`, CAP finds the test command from the project files: `cargo test` for `Cargo.toml`, `pytest` for Python projects, `npm test` for a `package.json` with a test script, `go test ./...` for `go.mod`.

### Result and exit code

| Headline | Status | Exit code |
|---|---|---|
| `DONE — the tests pass` | `verified` | 0 |
| `ANSWERED` (a question, nothing changed) | `answered` | 0 |
| `DONE, NOT VERIFIED` (files changed, the project has no tests) | `unverified` | 3 |
| `NOT DONE` | `tests_fail`, `step_limit`, `stuck`, `model_failed`, `context_full` | 1 |
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

Commands run as your user with the permission table above as the guard. Isolation by the operating system (Windows AppContainer, Linux Landlock) is phase 3 of the plan.

## Model servers

```
$ cap providers
name        model         context  key                 endpoint
ollama      qwen3:8b         8192  none needed         http://localhost:11434/v1
lmstudio    --model          8192  none needed         http://localhost:1234/v1
kau         qwen3.5-9b      16384  KAU_API_KEY         https://agent.kau.ac.kr/v1
openai      --model        128000  OPENAI_API_KEY      https://api.openai.com/v1
anthropic   --model        200000  ANTHROPIC_API_KEY   https://api.anthropic.com/v1
gemini      --model        128000  GEMINI_API_KEY      https://generativelanguage.googleapis.com/v1beta/openai
openrouter  --model        128000  OPENROUTER_API_KEY  https://openrouter.ai/api/v1
```

The key is read from the provider's environment variable or from `CAP_API_KEY`.

```sh
cap exec "add a --verbose flag" --provider ollama --model qwen3:8b
cap exec "why does test_login fail?" --mode read --provider openai --model <model>
cap exec "fix the failing test" --base-url http://localhost:8000/v1 --model my-model --context 16384
```

**Context on local servers.** Ollama and LM Studio load a model with a small context window unless told otherwise. CAP assumes 8192 tokens for them, so set the server to at least that (`OLLAMA_CONTEXT_LENGTH` or `num_ctx` for Ollama, the context length setting in LM Studio). When you set a larger window, pass the same number with `--context`.

## What is tested

- 75 unit and integration tests (`cargo test --workspace`) on Windows and Linux: the loop against a scripted model, the tools, the permission table, the test verdicts, context fitting, and the HTTP client against a local mock server.
- One real run against `qwen3.5-9b` on the KAU server: a one-file bug fix, 4 model calls, 19 s, tests passing afterwards.
- The other provider entries are checked against the mock server only.
- A comparison with other agents on the same model and tasks is phase 6 of the plan; no comparison has been measured yet.

## Layout

```
crates/cap-core   the agent as a library: loop, tools, edit, gate, permissions, context, model client
crates/cap        the command line program
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
