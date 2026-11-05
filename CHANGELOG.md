# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.10.2] - 2026-11-06

### Added
- Windows sandbox shell: `$VAR` and `${VAR}` become `%VAR%` (a value set earlier on the same line is put in directly; `$PWD` becomes `%CD%`).

### Changed
- The PyPI publish job runs only when the repository variable `PYPI_PUBLISH` is `true`; before PyPI is set up, a tag only builds and checks the package.

### Fixed
- Global options written after `exec` (`cap exec --mode auto "task"`) were taken as part of the task.
- `cap exec` without a terminal (scripts, CI) waited forever for an approval answer; it now declines right away and says so.

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
- `cap setup`: choose a model provider (OpenAI, Anthropic, Gemini, OpenRouter, Ollama, LM Studio, custom), save the key after a successful test call.
- Windows command sandbox with AppContainer (no WSL, no admin); Linux sandbox with Landlock.
- LangGraph coding loop with permission table, approvals, checkpoints (`/continue`), test gate, sub-agents.
- Claude Code / Codex compatibility: skills, commands, sub-agents, hooks, MCP (tools, resources, prompts), plugins.

[Unreleased]: https://github.com/juyoung020/cap-agent/compare/v0.10.2...HEAD
[0.10.2]: https://github.com/juyoung020/cap-agent/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/juyoung020/cap-agent/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/juyoung020/cap-agent/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/juyoung020/cap-agent/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/juyoung020/cap-agent/releases/tag/v0.8.0
