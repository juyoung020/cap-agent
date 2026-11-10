"""`cap view`: 스펙 탭 + 그래프 탭(Mermaid) 정적 HTML 한 장. 서버 없음."""

from __future__ import annotations

import html
import json
from pathlib import Path

import yaml

from .plan import render_mermaid
from .store import cap_dir

TEMPLATE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>CAP 뷰어</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
body{font-family:system-ui,'Malgun Gothic',sans-serif;margin:0;background:#f6f7f9;color:#222}
header{background:#1f2937;color:#fff;padding:10px 18px}
.tabs button{border:0;padding:8px 16px;margin-right:4px;border-radius:6px 6px 0 0;background:#e5e7eb;cursor:pointer}
.tabs button.on{background:#fff;font-weight:600}
.tabs{padding:10px 18px 0}
section{background:#fff;margin:0 18px 18px;padding:16px;border-radius:0 8px 8px 8px;display:none}
section.on{display:block}
pre{background:#f3f4f6;padding:10px;overflow:auto;white-space:pre-wrap}
li{margin:3px 0}
</style></head><body>
<header><b>CAP</b> — Code As Planned &nbsp; <small>__RUN__</small></header>
<div class="tabs"><button class="on" onclick="tab(0)">스펙</button><button onclick="tab(1)">그래프</button>
<button onclick="tab(2)">보고·장부</button></div>
<section class="on"><pre>__SPEC__</pre></section>
<section><div class="mermaid">__MERMAID__</div><h3>plan.yaml</h3><pre>__PLAN__</pre></section>
<section><pre>__REPORT__</pre><h3>장부(마지막 60줄)</h3><pre>__LEDGER__</pre></section>
<script>
mermaid.initialize({startOnLoad:false});
let drawn=false;
function tab(i){document.querySelectorAll('section').forEach((s,k)=>s.classList.toggle('on',k==i));
document.querySelectorAll('.tabs button').forEach((b,k)=>b.classList.toggle('on',k==i));
if(i==1&&!drawn){drawn=true;mermaid.run({querySelector:'.mermaid'});}}
</script></body></html>"""


def write_view(ws: Path, run_id: str | None) -> Path:
    d = cap_dir(ws)
    spec = (d / "spec.md").read_text(encoding="utf-8") if (d / "spec.md").is_file() else "(스펙 없음)"
    plan_txt = (d / "plan.yaml").read_text(encoding="utf-8") if (d / "plan.yaml").is_file() else ""
    mmd = "flowchart TD\n  A[계획 없음]"
    report, ledger = "(실행 기록 없음)", ""
    if run_id and (d / "runs" / f"{run_id}.mmd").is_file():
        mmd = (d / "runs" / f"{run_id}.mmd").read_text(encoding="utf-8")
    elif plan_txt:
        mmd = render_mermaid(yaml.safe_load(plan_txt))
    if run_id:
        rp = d / "runs" / f"{run_id}.report.md"
        if rp.is_file():
            report = rp.read_text(encoding="utf-8")
        lp = d / "runs" / f"{run_id}.jsonl"
        if lp.is_file():
            rows = lp.read_text(encoding="utf-8").splitlines()[-60:]
            out = []
            for r in rows:
                try:
                    x = json.loads(r)
                    out.append(f"{x['ts'][11:19]} {x['node']:<10} {x['type']:<14} " +
                               json.dumps({k: v for k, v in x.items() if k not in ('ts', 'run', 'node', 'type')},
                                          ensure_ascii=False)[:220])
                except (json.JSONDecodeError, KeyError):
                    pass
            ledger = "\n".join(out)
    page = (TEMPLATE.replace("__RUN__", html.escape(run_id or ""))
            .replace("__SPEC__", html.escape(spec)).replace("__MERMAID__", mmd)
            .replace("__PLAN__", html.escape(plan_txt)).replace("__REPORT__", html.escape(report))
            .replace("__LEDGER__", html.escape(ledger)))
    p = d / "view.html"
    p.write_text(page, encoding="utf-8")
    return p
