---
name: cap-delegate
description: Use when the user asks to hand a coding task to CAP, or wants a second agent to fix, implement or review code in the current folder under CAP's permission table, sandbox and test gate.
---

# Delegate a coding task to CAP

CAP runs as the MCP server `cap` (command `cap mcp-server`) with one tool, `run_task`.

1. Write the task as one self-contained request: what to change, which files or tests matter, what "done" means.
2. Call `run_task` with `task` and `mode`:
   - `chat` — read-only questions about the code.
   - `auto` — edits and commands; risky actions (deleting, installing, pushing, writing outside the folder) are declined because nobody is there to approve them.
   - `ask` — every edit and command needs approval, so almost everything is declined; use it only to see what CAP would try.
3. Read the JSON result: `status` (`done` / `escalated` / `failed`), `answer`, `changed` files and `verify_note`.
   Trust `verify_note` ("harness ran … passed") over the answer text — it comes from CAP running the tests itself.
4. If `status` is not `done`, report what CAP said it was stuck on instead of retrying the same task.

CAP needs `KAU_API_KEY` (or a `.env` with it) for its model server.
