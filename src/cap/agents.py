"""하위 에이전트(subagent) — 메인 에이전트가 곁가지 일을 맡기고 요약만 돌려받는다.

설계(docs/codex/09_multi_agent.md §14, docs/hermes/09_다중에이전트_목표루프.md §1):
- 동기형: 맡김 → 병렬 실행 → 결과 요약이 도구 결과로 바로 돌아온다(Codex V1 wait_agent 방식). 9B 에 맞다.
- 역할 3개: explore(읽기 전용 조사), review(읽기 전용 검토), worker(맡은 파일만 고침, Codex worker 의 '쓰기 범위 명시').
- 깊이 1: 하위 에이전트는 delegate 도구가 없다(재귀 금지). 동시 2개(LangGraph max_concurrency), 한 요청에 최대 6개.
- 부모는 자식의 중간 도구 호출·추론을 받지 않는다 — 짧은 요약만(Hermes §1-1). 요약은 3,000자로 자른다.
- 자식은 같은 코딩 루프 그래프(build_loop, mode="chat")를 따로 컴파일해 메모리 체크포인터로 돈다.
  승인은 부모의 승인 창구로 올라오고(누가 묻는지 표시), 동시에 둘이 묻지 않게 잠근다.
- 자식은 그림자 git 스냅샷을 찍지 않는다(동시 git 충돌 방지). 되돌리기는 부모 턴 시작 스냅샷이 맡는다.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

ROLES = ("explore", "review", "worker")
MAX_TASKS_PER_CALL = 3
MAX_CHILDREN_PER_TURN = 6
CHILD_MAX_TURNS = 15
SUMMARY_MAX = 3000
CONCURRENCY = 2

DELEGATE_SCHEMA = {"type": "function", "function": {
    "name": "delegate",
    "description": (
        "Hand independent side tasks to subagents that run in parallel and return short summaries. "
        "Roles: explore = read-only investigation of the code (where/how something works); "
        "review = read-only review of changes or code for bugs; "
        "worker = makes code changes, but ONLY in the files you list in `files` (give workers disjoint files). "
        "Use it only when the user asks for subagents/parallel work, or when there are 2-3 clearly independent "
        "investigations. Do the main path yourself; never delegate the whole request. Each task must contain all "
        "the context it needs — subagents do not see this conversation."),
    "parameters": {"type": "object", "properties": {"tasks": {"type": "array", "maxItems": MAX_TASKS_PER_CALL, "items": {
        "type": "object", "properties": {
            "role": {"type": "string", "enum": list(ROLES)},
            "task": {"type": "string", "description": "what to do and what to report back"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "worker only: files it may edit"}},
        "required": ["role", "task"]}}}, "required": ["tasks"]}}}

CHILD_SYSTEM = {
    "explore": ("You are a focused subagent (role: explore) working for the main coding agent. You can only read and "
                "search files; you cannot edit or run commands. Investigate exactly the task below."),
    "review": ("You are a focused subagent (role: review) working for the main coding agent. You can only read and "
               "search files; you cannot edit or run commands. Review exactly what the task asks for: real bugs, "
               "missing tests, risky changes. Cite file:line."),
    "worker": ("You are a focused subagent (role: worker) working for the main coding agent. You may edit ONLY these "
               "files: {files}. You are not alone in this codebase: other agents may be editing other files at the "
               "same time — never revert or touch changes outside your files. Do the task, then run the tests."),
}
CHILD_DONE = ("When done, reply WITHOUT a tool call with a short summary for the main agent: results first, then files "
              "you changed (if any) and problems. Use short bullets. Do not replay your steps — a long summary pushes "
              "the main agent's context out.")


def custom_agents(ws: Path) -> dict[str, dict]:
    """사용자 정의 하위 에이전트(Claude Code .claude/agents 와 같은 모양):
    ~/.cap/agents/<이름>.md, .cap/agents/<이름>.md — 맨 위 --- 안에 description:, tools: read|edit, 본문 = 지시문."""
    import re

    from .compat import agent_dirs
    out: dict[str, dict] = {}
    for d in agent_dirs(ws):
        for f in sorted(d.glob("*.md"))[:30]:
            text = f.read_text(encoding="utf-8", errors="replace")
            meta, body = {}, text
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
            if m:
                body = text[m.end():]
                for ln in m.group(1).splitlines():
                    k, sep, v = ln.partition(":")
                    if sep:
                        meta[k.strip().lower()] = v.strip().strip("'\"")
            name = re.sub(r"[^a-z0-9_-]", "-", (meta.get("name") or f.stem).lower())
            if name in ROLES or not body.strip():
                continue
            tools = meta.get("tools", "read").lower()
            # CAP: read|edit. Claude Code: "Read, Grep, Edit" 같은 도구 이름 목록 — 쓰기 도구가 있으면 쓰는 에이전트
            writes = tools in ("edit", "write", "all") or bool(re.search(r"\b(edit|write|multiedit|notebookedit)\b", tools))
            out.pop(name, None)
            out[name] = {"description": " ".join(meta.get("description", "").split())[:200], "writes": writes,
                         "prompt": body.strip()[:4000]}
    return out


def delegate_schema(custom: dict[str, dict]) -> dict:
    """사용자 정의 에이전트가 있으면 역할 목록과 설명에 더한 delegate 스키마."""
    if not custom:
        return DELEGATE_SCHEMA
    import copy
    s = copy.deepcopy(DELEGATE_SCHEMA)
    fn = s["function"]
    fn["parameters"]["properties"]["tasks"]["items"]["properties"]["role"]["enum"] = list(ROLES) + list(custom)
    shown = list(custom.items())[-8:]          # 16k 창: 설명은 8개까지(프로젝트 것이 뒤에 있어 먼저 남는다)
    fn["description"] += " Custom roles: " + "; ".join(
        f"{n} = {(c['description'] or 'custom agent')[:100]} "
        f"({'edits only the files you list' if c['writes'] else 'read-only'})" for n, c in shown) + \
        (f"; (+{len(custom) - len(shown)} more roles)" if len(custom) > len(shown) else "")
    return s


def validate_tasks(tasks: Any, ws: Path, mode: str, used: int,
                   custom: dict[str, dict] | None = None) -> tuple[list[dict], str]:
    """delegate 인자 검사 → (정리된 과제들, 오류 문장)."""
    custom = custom or {}
    if not isinstance(tasks, list) or not tasks:
        return [], "delegate needs a non-empty `tasks` list"
    if len(tasks) > MAX_TASKS_PER_CALL:
        return [], f"at most {MAX_TASKS_PER_CALL} tasks per delegate call"
    if used + len(tasks) > MAX_CHILDREN_PER_TURN:
        return [], f"subagent limit for this request reached ({MAX_CHILDREN_PER_TURN}). Solve the rest yourself."
    out, owned = [], set()
    for t in tasks:
        if not isinstance(t, dict) or (t.get("role") not in ROLES and t.get("role") not in custom) \
                or not str(t.get("task", "")).strip():
            return [], f"each task needs role ({'|'.join([*ROLES, *custom])}) and a non-empty task"
        files = [str(f).replace("\\", "/").lstrip("./") for f in (t.get("files") or [])]
        writes = t["role"] == "worker" or bool(custom.get(t["role"], {}).get("writes"))
        if writes:
            if mode in ("chat", "plan"):
                return [], f"workers edit files, which is not allowed in {mode} mode. Use explore/review."
            if not files:
                return [], "a worker task must list the files it may edit in `files`"
            for f in files:
                p = (ws / f).resolve()
                if ws.resolve() not in p.parents:
                    return [], f"worker file outside the workspace: {f}"
                if f in owned:
                    return [], f"two workers were given the same file ({f}); give workers disjoint files"
                owned.add(f)
        item = {"role": t["role"], "task": str(t["task"]).strip(), "files": files if writes else [], "writes": writes}
        if t["role"] in custom:
            item["prompt"] = custom[t["role"]]["prompt"]
        out.append(item)
    return out, ""


def child_prompt(role: str, files: list[str], custom_prompt: str = "", writes: bool = False) -> str:
    if custom_prompt:
        rule = (f"You may edit ONLY these files: {', '.join(files)}. Other agents may be editing other files — never "
                "touch changes outside your files." if writes else "You can only read and search files.")
        return (f"You are a subagent (role: {role}) working for the main coding agent. {rule}\n\n{custom_prompt}\n"
                + CHILD_DONE)
    return CHILD_SYSTEM[role].format(files=", ".join(files) or "(none)") + "\n" + CHILD_DONE


def run_child(build: Callable[..., Any], *, ws: Path, parent_ledger, payload: dict, mode: str, test_cmd: str,
              ask_human: Callable[[dict], str] | None, should_stop: Callable[[], bool] | None,
              system_extra: str) -> dict:
    """하위 에이전트 하나를 끝까지 돌리고 결과 요약을 돌려준다(부모 그래프의 subagent 노드가 부른다)."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from .store import Ledger
    role, label = payload["role"], payload["label"]
    writes = payload.get("writes", role == "worker")
    child_mode = mode if writes else "chat"
    led = Ledger(ws, f"{parent_ledger.run_id}-{label}")
    graph = build(ws=ws, ledger=led, test_cmd=test_cmd, frozen=set(), preapproved=set(), decisions=[],
                  printer=lambda s: None, checkpointer=InMemorySaver(), mode="chat",
                  approval={"mode": child_mode},
                  system_extra=child_prompt(role, payload["files"], payload.get("prompt", ""), writes) + "\n\n" + system_extra,
                  ui=None, child=True, write_scope=set(payload["files"]) if writes else None,
                  max_turns=CHILD_MAX_TURNS, should_stop=should_stop)
    cfg = {"configurable": {"thread_id": label}, "recursion_limit": 300}
    inp: Any = {"goal": payload["task"]}
    t0, status, err = time.perf_counter(), "", ""
    try:
        while True:
            graph.invoke(inp, cfg)
            st = graph.get_state(cfg)
            intr = [i.value for t in st.tasks for i in t.interrupts]
            if not intr:
                break
            q = intr[0]
            if q.get("kind") == "approval" and ask_human is not None:
                ans = ask_human({**q, "agent": label})
            else:
                ans = "abort"          # 하위 에이전트가 막히면 사람에게 묻지 않고 멈춘다 — 부모가 판단
            inp = Command(resume=ans)
    except KeyboardInterrupt:
        status, err = "interrupted", "stopped by the user"
    except RuntimeError as exc:
        status, err = "error", str(exc)[:300]
    v = graph.get_state(cfg).values or {}
    summary = ""
    for m in reversed(v.get("messages") or []):
        if m.get("role") == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
            summary = m["content"].strip()
            break
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX] + f"\n…(cut; full trace in .cap/runs/{led.run_id}.jsonl)"
    calls = sum(1 for r in led.read() if r.get("type") == "llm_call")
    status = status or {"done": "done", "failed": "stopped at its limit", "escalated": "stuck"}.get(v.get("status"), v.get("status") or "?")
    led.log("subagent_end", status=status, summary=summary[:500])
    return {"call_id": payload["call_id"], "idx": payload["idx"], "label": label, "role": role,
            "task": payload["task"], "status": status, "error": err, "summary": summary or "(no summary)",
            "changed": list(v.get("turn_changed") or []), "file_hashes": dict(v.get("file_hashes") or {}),
            "verify_note": v.get("verify_note", ""), "calls": calls, "seconds": round(time.perf_counter() - t0, 1),
            "trace": f".cap/runs/{led.run_id}.jsonl"}


def result_text(r: dict) -> str:
    head = f"[{r['label']} · {r['role']} · {r['status']} · {r['calls']} model calls · {r['seconds']:.0f}s]"
    extra = []
    if r["changed"]:
        extra.append("Files changed: " + ", ".join(r["changed"]))
    if r.get("verify_note"):
        extra.append("Tests: " + r["verify_note"])
    if r.get("error"):
        extra.append("Error: " + r["error"])
    return "\n".join([head, r["summary"], *extra])
