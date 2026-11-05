"""처음 설정(cap setup): 공급자 고르기 → 키(성공했을 때만 저장) → 모델 → 시험 호출 → 기본으로 저장."""
import http.server
import json
import os
import threading

import pytest

from cap import cli, compat, llm
from cap import setup as SU


@pytest.fixture
def llm_state(monkeypatch):
    for k in ("BASE_URL", "MODEL", "CONTEXT_WINDOW", "_client", "_key"):
        monkeypatch.setattr(llm, k, getattr(llm, k))
    monkeypatch.setattr(llm, "PROVIDER", dict(llm.PROVIDER))
    for env in ("KAU_API_KEY", "CAP_API_KEY", "OPENAI_API_KEY", "CAP_CUSTOM_API_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def server():
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"object": "list", "data": [{"id": "tiny-a", "object": "model"},
                                                          {"id": "tiny-b", "object": "model"}]}).encode()
            self._send(200, body)

        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization", "")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "bad" in seen["auth"]:
                return self._send(401, b'{"error": {"message": "invalid api key"}}')
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": seen["body"]["model"],
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "OK"}}]}).encode()
            self._send(200, body)

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1", seen
    srv.shutdown()


def test_setup_saves_key_and_default_only_after_a_working_call(llm_state, server, monkeypatch):
    url, seen = server
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    out = []
    assert SU.run_setup("custom", model="tiny-a", key="bad-key", base_url=url, out=out.append) == 1
    assert "Test call failed" in "\n".join(out) and SU.load_auth() == {}                 # 실패: 키 저장 안 함
    assert "provider" not in compat.settings()
    out.clear()
    assert SU.run_setup("custom", model="tiny-a", key="sk-good", base_url=url, out=out.append) == 0
    assert seen["auth"] == "Bearer sk-good" and seen["body"]["model"] == "tiny-a"
    assert SU.load_auth()["custom"]["api_key"] == "sk-good"
    st = compat.settings()
    assert st["provider"] == "custom" and st["model"] == "tiny-a" and st["model_providers"]["custom"]["base_url"] == url
    if os.name == "nt":                                                                   # 키 파일은 이 계정만
        import subprocess
        acl = subprocess.run(["icacls", str(SU.auth_path())], capture_output=True, text=True, encoding="mbcs").stdout
        assert os.environ["USERNAME"].lower() in acl.lower() and "Users:" not in acl and "Everyone" not in acl


def test_interactive_setup_lists_models_and_asks(llm_state, server, monkeypatch):
    url, seen = server
    answers = iter(["8", url, "tiny-b"])                                    # custom → URL → 모델
    out = []
    code = SU.run_setup(ask=lambda q: next(answers), secret=lambda q: "sk-x", out=out.append)
    assert code == 0 and "tiny-a, tiny-b" in "\n".join(out) and seen["body"]["model"] == "tiny-b"
    assert compat.settings()["model"] == "tiny-b"


def test_saved_default_is_applied_and_env_key_wins(llm_state, monkeypatch):
    SU.save_settings(provider="openai", model="gpt-x")
    SU.save_key("openai", "sk-stored")
    SU.apply_user_default()
    assert llm.PROVIDER["name"] == "openai" and llm.MODEL == "gpt-x" and llm._load_key() == "sk-stored"
    llm.use_provider("openai", "gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert llm._load_key() == "sk-env"


def test_version_and_no_key_message(llm_state, monkeypatch, capsys):
    assert cli.main(["--version"]) == 0 and capsys.readouterr().out.startswith("cap ")
    monkeypatch.delenv("CAP_SKIP_SETUP_CHECK")
    llm.use_provider("openai")
    assert cli.main(["exec", "hi"]) == 2
    assert "cap setup" in capsys.readouterr().out
