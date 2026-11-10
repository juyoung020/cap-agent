from cap import plan as P
from cap.spec import parse

SPEC = """# t

## 요구사항
- [기능] 나눗셈 수정  ⟨check: python -m pytest -q⟩
- [성능] 빠르게  ⟨check: python bench.py⟩ ⟨target: <= 0.5⟩
- [산출물] README 사용법
- [기능] 끝나면 커밋·푸시

## 결정
- [규칙] numpy import 금지
"""


def test_spec_parse():
    sp = parse(SPEC)
    assert len(sp.requirements) == 4 and len(sp.decisions) == 1
    assert sp.requirements[1].target == "<= 0.5"
    assert sp.test_cmd() == "python -m pytest -q"
    assert [r.id for r in sp.manual] == ["R3", "R4"]


def test_attach_rules():
    sp = parse(SPEC)
    plan, notes = P.attach({"nodes": [{"id": "fix", "kind": "code", "goal": "fix divide"}]}, sp)
    kinds = [n["kind"] for n in plan["nodes"]]
    assert kinds[0] == "eval_check" and kinds[1] == "code" and kinds[2] == "test"
    assert "bench" in kinds and "ask" in kinds
    assert kinds[-4:] == ["cleanup", "docs", "changelog", "git"]
    git = plan["nodes"][-1]
    assert git["approve"] == "ask" and git["goal"] == "commit_push"
    assert all(n.get("auto") for n in plan["nodes"] if n["id"] != "fix")
    assert not P.validate(plan)


def test_attach_is_idempotent():
    sp = parse(SPEC)
    p1, _ = P.attach({"nodes": [{"id": "fix", "kind": "code", "goal": "g"}]}, sp)
    p2, notes = P.attach(p1, sp)
    assert p1["nodes"] == p2["nodes"] and not notes


def test_eval_loop_gets_eval_check_before_body():
    sp = parse(SPEC)
    raw = {"nodes": [{"id": "fix", "kind": "code", "goal": "g"}, {"id": "t", "kind": "test", "cmd": "pytest"},
                     {"id": "loop", "kind": "eval_loop", "body": ["fix", "t"], "threshold": 1.0, "max_iters": 3}]}
    plan, _ = P.attach(raw, sp)
    ids = [n["id"] for n in plan["nodes"]]
    assert ids.index("check") < ids.index("fix")
    assert not P.validate(plan)
    assert [n["id"] for n in P.main_chain(plan)][:2] == ["check", "loop"]


def test_removed_by_user_not_reattached():
    sp = parse(SPEC)
    plan, _ = P.attach({"nodes": [{"id": "fix", "kind": "code", "goal": "g"}],
                        "removed_by_user": ["docs", "bench"]}, sp)
    kinds = [n["kind"] for n in plan["nodes"]]
    assert "docs" not in kinds and "bench" not in kinds and "cleanup" in kinds


def test_validate_errors():
    assert P.validate({"nodes": [{"id": "a", "kind": "rm_rf"}]})
    assert P.validate({"nodes": [{"id": "a", "kind": "code"}]})  # goal 없음
    assert P.validate({"nodes": [{"id": "a", "kind": "eval_loop", "body": ["zz"]}]})
    assert P.validate({"nodes": [{"id": "a", "kind": "code", "goal": "x"}] * 2})  # 중복 id
    assert P.validate({"nodes": [{"id": f"n{i}", "kind": "code", "goal": "x"} for i in range(21)]})


def test_render_text_and_mermaid():
    sp = parse(SPEC)
    plan, _ = P.attach({"nodes": [{"id": "fix", "kind": "code", "goal": "fix divide"}]}, sp)
    txt = P.render_text(plan)
    assert "fix" in txt and "(자동)" in txt
    mm = P.render_mermaid(plan, {"fix": "done"})
    assert mm.startswith("flowchart TD") and "style fix fill" in mm


def test_mentions():
    assert P.mentions("성능 측정 빼줘", "bench")
    assert not P.mentions("성능 측정 빼줘", "test")


def test_vague_task_gets_code_generated_question():
    from cap.spec import DROP, apply_answer, vague_questions
    sp = parse("# t\n\n## 요구사항\n- [성능] power 빠름  ⟨check: python b.py⟩ ⟨target: <= 0.5⟩\n")
    qs = vague_questions(sp, "큰 수도 적당히 빠르게", set())
    assert qs and qs[0]["options"][:3] == ["<= 0.5", "<= 1", "<= 0.25"] and qs[0]["options"][3] == DROP
    apply_answer(sp, "R1", "<= 1", "target")
    assert sp.requirements[0].target == "<= 1"
    apply_answer(sp, "R1", DROP, "target")
    assert not sp.requirements
    assert not vague_questions(parse("# t\n\n## 요구사항\n- [성능] x  ⟨check: a⟩ ⟨target: <= 1⟩\n"), "1초 이내", set())


def test_insert_snippet_into_sections():
    from cap.spec import insert_snippet
    base = "# t\n\n## 요구사항\n- [기능] a  ⟨check: x⟩\n\n## 결정\n- [규칙] b\n\n<!-- task: t -->\n"
    out = insert_snippet(base, "## 요구사항\n- [기능] new req\n## 결정\n- [규칙] numpy import 금지\n")
    sp = parse(out)
    assert [r.text for r in sp.requirements] == ["a", "new req"]
    assert [d.text for d in sp.decisions] == ["b", "numpy import 금지"]
    assert out.index("numpy import 금지") < out.index("<!-- task")


def test_greenfield_eval_check_after_first_green():
    sp = parse(SPEC)
    plan, _ = P.attach({"nodes": [{"id": "build", "kind": "code", "goal": "g"}]}, sp, has_tests=False)
    kinds = [n["kind"] for n in plan["nodes"]]
    assert kinds[:2] == ["code", "test"]
    assert kinds.index("test") < kinds.index("eval_check") < kinds.index("cleanup")
