# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

CAP is being rewritten in Rust (`0.15.0-alpha.1`). The Python version ends at 0.14.0 and stays installable from the tag `v0.14.0`.

### Added
- Rust workspace with two crates: `cap-core` (the agent as a library) and `cap` (the command line program, one executable).
- `cap` / `cap chat`: a conversation in a project folder. The model's text is shown while it arrives, each tool call and result is one short line, and every turn ends with the verdict, the changed files, the steps, the time and the context in use. Slash commands: `/help`, `/status`, `/mode`, `/model`, `/clear`, `/skills`, `/mcp`, `/quit`. A line that ends with `\` continues in the next line.
- `cap exec "task"`: run one task in a project folder and exit, with `--json` and `--trace`. Exit code 0 for `verified` and `answered`, 3 for `unverified`, 1 for an unfinished run, 2 for a setup error, 130 for an interrupted run.
- Options shared by `cap` and `cap exec`: `-C`, `--test`, `--no-test`, `--mode read|edit|auto`, `--max-steps`, `--provider`, `--model`, `--base-url`, `--context`, `--no-stream`, `--strict-tools`, `--tools small|standard|full`, `--sandbox auto|on|off`.
- `cap providers`: the model servers known by name (Ollama (default), LM Studio, KAU, OpenAI, Anthropic, Gemini, OpenRouter), all through the OpenAI-compatible format. The key comes from the provider's environment variable or `CAP_API_KEY`.
- Settings file (`%APPDATA%\cap\config.toml`, `~/.config/cap/config.toml`, or the path in `CAP_CONFIG`): defaults for the options above, isolation folders, providers of your own and MCP servers. A flag wins over an environment variable, which wins over the file. Unknown keys are reported as errors. `cap config` shows the settings in force and where the file lives; a key is shown by name and as set or not set.
- The loop: a reply without a tool call counts as a "done" claim and the tests decide. Failing output goes back to the model; four rejected claims or the same failure three times end the run as `tests_fail`. A passing result is reused until a file changes or a command other than a read-only one runs.
- Tools: `read_file`, `list_files`, `search`, `replace_lines`, `edit_file`, `write_file`, `run_tests`, `run`, `note`, `use_skill`, plus the tools of configured MCP servers. Tool and argument names used by other agents are accepted.
- Edits by line number (`replace_lines`): the model sends a line range and the new lines. The harness applies the call only to lines the model has seen in their current state; for other lines it answers with those lines, so the same call works when it is sent again. After a change the answer shows the lines as they are now. Line numbers copied into the new text are dropped, and new lines that come without any indentation get the indentation of the lines they replace.
- Tool sets by context window: `small` (up to 16 384 tokens: six tools with short descriptions), `standard` (up to 64 000: adds `list_files` and `edit_file`), `full` (adds `note`). `--tools` picks one by hand.
- Isolation of commands by the operating system. Linux uses Landlock: a command reads the system and the toolchains and writes only to the project, the temp folders and the build caches. Windows uses an AppContainer: a command opens only the folders handed over with `cap sandbox setup` and has no network. `cap sandbox setup|undo|status` manages what was handed over. `--sandbox auto` (default) isolates where it can and says so where it cannot; `on` refuses to start without isolation.
- Streaming: text is shown as the server sends it; `--no-stream` waits for the complete reply.
- Ctrl-C stops the running turn, ends the commands it started and reports `interrupted`; the files changed so far are listed.
- Work state board: the last lines of every request say the step, the files changed and read, the state of the tests, the model's own notes and the next step when it is clear, in at most 400 characters. It is added to the request copy only, so the stored conversation and the prompt prefix stay the same between calls.
- Code map: the functions and types of each source file are listed next to the file list in the first prompt (Python, Rust, JavaScript and TypeScript, Go, Java, Kotlin, C#, C and C++).
- The harness works ahead: files named in the task are put into the first prompt, and after a change the tests start on their own. The result of a fast suite arrives with the tool result; a slow suite keeps running while the model writes. `eager_tests = false` in the settings file turns the test part off.
- `--strict-tools`: every reply has to be a tool call (`tool_choice: "required"`), with a `done` tool to finish. A server that refuses the option gets the request again without it.
- Skills: folders with a `SKILL.md` under `.cap/skills`, `.claude/skills` and `.agents/skills`, in the project and in the home folder. The prompt carries each name and one line of description; the body is read with `use_skill`. `cap skills` lists them.
- MCP servers over stdin and stdout, set in the settings file. Their tools are offered as `mcp__<server>__<tool>`, and a call asks first unless the mode is `auto` or the server is marked `allow = true`. `cap mcp` starts the servers and lists their tools.
- Help for small models: broken arguments are explained, a call that failed twice with the same arguments is cut short, an unchanged file is not sent twice, an announcement ("I will now fix…") is told apart from an answer, empty and cut-off replies are retried, and tool calls written out as text are read as calls.
- Permission table with three modes. Paths outside the project, files that hold keys and destructive commands are refused in every mode; network, package and git-history commands ask first; changing an existing test file asks first.
- Test verdicts for Cargo, pytest, npm and Go projects, with the test command found from the project files.
- Context fitting: token estimate corrected by the server's own count, old tool output shortened first, then the oldest rounds dropped.
- HTTP client: retries on 429 and server errors, errors told apart (refused key, missing model, full context, busy server, unreachable server), the key removed from error text, `<think>` text removed.
- Project instructions: `AGENTS.md` or `CAP.md` at the top of the project goes into the first prompt.
- `docs/PLAN.md` (phases and what "better than other agents" will mean) and `docs/PHILOSOPHY.md` (the five principles).
- 153 tests on Windows and 152 on Linux: unit tests, the loop and whole conversations against a scripted model, the HTTP client against a local mock server, and the isolation against real commands (Landlock on Linux, AppContainer on Windows).
- Single trial runs of the built program with `qwen3.5-9b` on the KAU server, isolation off: `cap exec` on a one-line bug fix ended as `verified` in 4 model calls in each of two runs (17.9 s with 9301 tokens in and 196 out; 25.1 s with 9482 in and 280 out), with the harness adding the indentation the model left out; a piped `cap chat` answered a question in 2 calls and ran its slash commands. The effect of each feature on a real model has not been measured, and no comparison with other agents has been run for the Rust version.

### Changed
- The Python sources, tests, plugin files and documents moved to `archive/python`.
- CI formats, lints, tests and builds the Rust workspace on Ubuntu and Windows and checks it with Rust 1.88. The tests fail there when the isolation cannot be exercised (`CAP_REQUIRE_SANDBOX=1`).

### Removed
- The PyPI release workflow.

## [0.14.0] - 2026-11-10

### Added
- Copied-lines note: when an `edit_file` call sends an `old` of eight or more lines and copies at least 70% of them unchanged into `new`, the edit is applied and the tool result then shows a shorter `old`/`new` that gives the same file. The harness shows it only after applying that shorter edit itself and getting an identical result. Rewriting a file of 20 or more lines with `write_file` while at least 80% of it stays the same gets a note to use `edit_file` with only the changed lines. Up to three notes per request; `CAP_ABLATION=no_echo` turns them off. It comes from a `qwen3.5-9b` trace where one import line was changed by sending almost the whole file once as `old` and once as `new` (about 640 tokens, 51 s). Checked with 21 unit tests only.

### Changed
- In an `edit_file` batch (`edits`), an item whose `old` and `new` are identical is skipped and the rest are applied; the result says which item was skipped. An item that is not found in the file still leaves the file untouched, as before. In the trace, a five-item batch (138 s to generate) was refused as a whole because of one such item.
- A single edit with identical `old` and `new` answers "this changes nothing; if this part is already the way you want it, go on to the next step". It carries no closest-lines list and does not count as an edit failure.
- `write_file` with exactly the content the file already has writes nothing and answers that the file already has this content.

## [0.13.0] - 2026-11-10

### Added
- Mid-run test check: after six code edits in a row without a test run, the harness runs the tests itself and appends the result (pass or fail, failing test names, output) to that tool result. It costs no extra model call. Documentation edits are not counted, and the count restarts when the model runs the tests.
- Comparison with another agent on the same model, tasks and grading (measured in the source repo): Aider 0.86.2 (Aider-AI) with `qwen3.5-9b`, four tasks, one run each. CAP solved 4/4, Aider 2/4 (the other two stopped at its limit of three fix attempts); neither reported a false "done".
- Re-measured after the change (source repo, two tasks, one run): `tier` passed with the same 13 model calls and 187 s as before, with one mid-run check in between. `dedup` did not finish within the 15-minute limit; it was unsolved before the change as well.

### Fixed
- Windows sandbox shell: a multi-line command ran only its first line and came back empty. Lines now run in order, and the body of a multi-line `python -c "..."` or a heredoc (`<<EOF`) is moved to a file under `.cap/sbx/inline/` and run from there. Other multi-line quoted arguments are refused with the reason.

## [0.12.0] - 2026-11-08

### Added
- Long-running jobs, `cap go "task"`: spec draft → autonomy preset (safe, normal, bold) → stages → one summary screen to confirm → run. Each stage gets a fresh context (a short work packet), writes and locks its tests before implementing, changes approach on each retry (as is → failure cause + smallest fix → one file at a time), re-checks earlier stages and rolls back if it broke them, and commits. At the end every check is run again for the report.
- `--detach` (a supervisor restarts a dead run from its checkpoint; sleep is blocked while running), `--until` / `--hours` deadline, a git worktree work branch, and a one-line status board in `cap go status`.
- Question box `cap inbox`: an action that needs approval is declined and turned into a question, and a stage that failed three times waits while other stages keep running. Unanswered questions take their default after 30 minutes. Phone notifications (ntfy) with answer buttons.
- A requirement whose check names several test files is split per file, and functional requirements in a project without tests get one test file each.

### Fixed
- Sub-commands such as `cap spec` and `cap graph` failed at exit with a name error for `llm`.

## [0.11.0] - 2026-11-06

### Added
- `CAP.md` project instructions, read like Claude Code's `CLAUDE.md`: `CAP.md` and `CAP.local.md` (personal) in the workspace, `~/.cap/CAP.md` for all projects, and `CAP.md` in subfolders when CAP first touches files there. CAP's own file is read last in each folder, so it wins. Writing these files always asks a human, like other instruction files.

## [0.10.2] - 2026-11-06

### Added
- Windows sandbox shell: `$VAR` and `${VAR}` become `%VAR%` (a value set earlier on the same line is put in directly; `$PWD` becomes `%CD%`).

### Changed
- The PyPI publish job runs only when the repository variable `PYPI_PUBLISH` is `true`; before PyPI is set up, a tag only builds and checks the package.

### Fixed
- Global options written after `exec` (`cap exec --mode auto "task"`) were taken as part of the task.
- `cap exec` without a terminal (scripts, CI) waited forever for an approval answer; it now declines right away and says so.
- Windows sandbox: the file used in place of NUL is now cleaned up in the right place (TEMP inside the sandbox is a sandbox-only folder that Windows provides).

## [0.10.1] - 2026-11-06

### Fixed
- On some Windows machines (GitHub Actions runners) every sandboxed command failed with `Access is denied`: processes inside the AppContainer could not open the NUL device. `chcp` output, `> /dev/null` and Python's `os.devnull` inside the sandbox now go to a file in the sandbox temp folder, and a failing `chcp` no longer stops the command.
- `/resume` picking the older of two sessions opened in the same second.
- Linux: a Windows-only labelling helper called `icacls`; a string escape warning in `sandbox.py`.

## [0.10.0] - 2026-11-05

### Added
- Benchmark of 11 hard multi-file tasks (`eval/make_bench.py` in the source repo), each self-checked for solvability. With `qwen3.5-9b`: CAP 8/11 with no false "done" (3 handed to a human), a single call without the agent loop 1/11.
- Fixes from running CAP on a real project: failure summary at the top of failing test output, failure lines and the latest test result survive compaction, a definition map is kept for compacted file reads, a note when the same file is read again and again, a hint to search the whole project, mid-turn compaction at 75% instead of 60%.
- `/testcmd` to set the project's test command (stored per project in your home folder).
- Common Unix commands inside the Windows sandbox (`ls cat head tail grep wc find rm cp mv mkdir -p touch pwd which diff sed -n`, `dir`) and command-line translation (`export`, `/dev/null`, single quotes, `;`).
- Optional model review of approvals for capable models (`"auto_review": true`).
- New-version notice (once a day, can be turned off).
- Linux support in CI; GitHub Actions test matrix (Windows and Linux, Python 3.11 and 3.12), build check, and PyPI release on tags via Trusted Publishing.

### Fixed
- Windows sandbox: executables inside a prepared workspace (e.g. the project's `.venv\Scripts\python.exe`) inherited a Low integrity label and failed when you ran them yourself.

## [0.9.0] - 2026-11-05

### Added
- Repairs wrong tool and argument names from small models (`str_replace`, `old_string`, `bash`, `command`, …); escalates after three rounds without a valid call.
- Nudges once when a reply says "I'll do X" but makes no tool call; `/goal` stops when nothing progresses.
- Editing: Unicode punctuation/space normalisation stage, double-escape detection, pieced-together reads count as a full read, applied diff shown after an edit; `write_file` strips copied line numbers and refuses broken JSON/TOML/YAML before writing.
- Commands run with the project's own `.venv` Python; quiet environment (no colour, no pager); `run` longer than 30 s moves to the background; background output returns only new output.
- Helper model calls never use thinking mode; call errors are classified with a fix (bad key, unknown model, server not running); replies that repeat themselves are cut early.
- Test command and instruction files are re-checked every turn; large MCP schemas are compacted and large MCP results spilled to `.cap/outputs`.
- Mid-turn compaction first shortens old tool outputs and only then summarises.

### Fixed
- A test file failing at import no longer makes CAP claim other tests "passed before"; baseline and `/goal` test runs now use the sandbox.
- The auto-compaction threshold follows the model's context window.
- Windows sandbox: project `.venv` (including uv hard-linked files) and its base Python are readable.

## [0.8.0] - 2026-11-05

### Added
- First public release.
- `cap setup` (`cap login`): choose a model provider (OpenAI, Anthropic, Gemini, OpenRouter, Ollama, LM Studio, custom), save the key after a successful test call (`~/.cap/auth.json`, readable by your account only).
- `cap sandbox [status|setup|setup wsl|undo]` and `cap --version`.
- Windows command sandbox with AppContainer (no WSL, no admin); Linux sandbox with Landlock.
- LangGraph coding loop with permission table, approvals, checkpoints (`/continue`), test gate, sub-agents.
- Claude Code / Codex compatibility: skills, commands, sub-agents, hooks, MCP (tools, resources, prompts), plugins.

### Fixed
- Windows sandbox: in an environment installed with uv or pipx, Python inside the sandbox could not read its packages (hard-linked cache files do not inherit folder permissions, so each file is granted access).

[Unreleased]: https://github.com/juyoung020/cap-agent/compare/v0.14.0...HEAD
[0.14.0]: https://github.com/juyoung020/cap-agent/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/juyoung020/cap-agent/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/juyoung020/cap-agent/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/juyoung020/cap-agent/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/juyoung020/cap-agent/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/juyoung020/cap-agent/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/juyoung020/cap-agent/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/juyoung020/cap-agent/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/juyoung020/cap-agent/releases/tag/v0.8.0
