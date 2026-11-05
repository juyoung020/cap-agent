# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.0] - 2026-11-05

### Added
- First public release.
- `cap setup`: choose a model provider (OpenAI, Anthropic, Gemini, OpenRouter, Ollama, LM Studio, custom), save the key after a successful test call.
- Windows command sandbox with AppContainer (no WSL, no admin); Linux sandbox with Landlock.
- LangGraph coding loop with permission table, approvals, checkpoints (`/continue`), test gate, sub-agents.
- Claude Code / Codex compatibility: skills, commands, sub-agents, hooks, MCP (tools, resources, prompts), plugins.

[Unreleased]: https://github.com/juyoung020/cap-agent/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/juyoung020/cap-agent/releases/tag/v0.8.0
