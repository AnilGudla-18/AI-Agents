"""Background mode: requests return at once, agents keep working, progress is polled."""
import asyncio
import json
import tempfile
import time

from fastapi.testclient import TestClient

from aisew.api.app import create_app
from aisew.config import Settings
from aisew.llm.fake import FakeLLM

from test_pipeline_api import (
    BA, DBA, DEV, MGR, QA, LONG_REQ, ba_reply, ids, routes,
)


class SlowFake(FakeLLM):
    """Every model call takes `delay` seconds, like a slow local model."""

    def __init__(self, delay=0.15, **kw):
        super().__init__(**kw)
        self.delay = delay

    async def generate(self, **kw):
        await asyncio.sleep(self.delay)
        return await super().generate(**kw)


def make_bg(delay=0.15, **overrides):
    llm = SlowFake(delay=delay, routes=routes(**overrides))
    app = create_app(llm=llm, database_url="sqlite://", workspace_dir=tempfile.mkdtemp(), background=True, sandbox=None)
    return app, llm


def wait_for(client, rid, done, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/requirements/{rid}").json()
        if done(data):
            return data
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting; last state: {json.dumps(data)[:600]}")


def idle(d):
    return d["running"] == []


def test_create_returns_immediately_while_the_agent_is_still_running():
    app, _ = make_bg(delay=0.3)
    with TestClient(app) as client:
        started = time.time()
        r = client.post("/requirements", json={"text": LONG_REQ}).json()
        assert time.time() - started < 0.25            # did not wait for the model
        assert r["artifacts"] == [] and r["status"] == "Running"
        assert [x["agent"] for x in r["running"]] == ["BA"]
        assert r["needs_retry"] == []                    # running is not a failure

        data = wait_for(client, r["id"], lambda d: d["artifacts"] and idle(d))
        assert data["status"] == "InReview" and data["awaiting"][0]["agent"] == "BA"


def test_approve_starts_three_stages_in_the_background_and_then_the_developer():
    app, _ = make_bg()
    with TestClient(app) as client:
        rid = client.post("/requirements", json={"text": LONG_REQ}).json()["id"]
        data = wait_for(client, rid, lambda d: d["artifacts"] and idle(d))
        r = client.post(f"/artifacts/{ids(data)['BA']}/approve").json()
        assert sorted(x["agent"] for x in r["running"]) == ["DBA", "Manager", "QA"]
        assert r["status"] == "Running"

        data = wait_for(client, rid, lambda d: len(d["artifacts"]) == 4 and idle(d))
        for agent in ("DBA", "Manager", "QA"):
            client.post(f"/artifacts/{ids(data)[agent]}/approve")
        data = wait_for(client, rid, lambda d: "Developer" in ids(d) and idle(d))
        assert [a["agent"] for a in data["awaiting"]] == ["Developer"]

        done = client.post(f"/artifacts/{ids(data)['Developer']}/approve").json()
        assert done["workspace"] is None or done["status"] in ("Complete", "InReview")
        data = wait_for(client, rid, lambda d: d["status"] == "Complete")
        assert data["workspace"]


def test_the_same_stage_cannot_be_started_twice():
    app, _ = make_bg(delay=0.4, **{BA: [ba_reply(), ba_reply(summary="shorter")]})
    with TestClient(app) as client:
        rid = client.post("/requirements", json={"text": LONG_REQ}).json()["id"]
        data = wait_for(client, rid, lambda d: d["artifacts"] and idle(d))
        aid = data["awaiting"][0]["artifact_id"]

        first = client.post(f"/artifacts/{aid}/feedback", json={"feedback": "Make the summary shorter"})
        assert first.status_code == 200 and first.json()["running"][0]["agent"] == "BA"

        second = client.post(f"/artifacts/{aid}/feedback", json={"feedback": "Make it even shorter"})
        assert second.status_code == 409 and "already running" in second.json()["detail"]

        # /run must not start a second copy either
        again = client.post(f"/requirements/{rid}/run").json()
        assert [x["agent"] for x in again["running"]] == ["BA"]

        wait_for(client, rid, lambda d: len(d["artifacts"]) == 2 and idle(d))


def test_a_failure_in_the_background_is_logged_and_can_be_retried():
    app, _ = make_bg(**{QA: ["garbage", "garbage", routes()[QA][0]]})
    with TestClient(app) as client:
        rid = client.post("/requirements", json={"text": LONG_REQ}).json()["id"]
        data = wait_for(client, rid, lambda d: d["artifacts"] and idle(d))
        client.post(f"/artifacts/{ids(data)['BA']}/approve")
        data = wait_for(client, rid, lambda d: idle(d) and len(d["artifacts"]) == 3)
        assert data["needs_retry"] == ["QA"] and data["status"] != "Running"
        assert any(e["event"] == "QAFailed" for e in data["audit_log"])

        client.post(f"/requirements/{rid}/run")
        data = wait_for(client, rid, lambda d: "QA" in ids(d) and idle(d))
        assert data["needs_retry"] == []


def test_a_failed_reply_in_the_background_is_logged_not_lost():
    qs = [{"question": "Which gateway?", "blocking": True}]
    app, _ = make_bg(**{BA: [ba_reply(open_questions=qs), "garbage", "garbage"]})
    with TestClient(app) as client:
        rid = client.post("/requirements", json={"text": LONG_REQ}).json()["id"]
        data = wait_for(client, rid, lambda d: d["artifacts"] and idle(d))
        r = client.post(f"/artifacts/{ids(data)['BA']}/reply",
                        json={"answers": [{"question": "Which gateway?", "answer": "Stripe"}]})
        assert r.status_code == 200
        data = wait_for(client, rid, lambda d: idle(d) and any(e["event"] == "BAFailed" for e in d["audit_log"]))
        assert data["artifacts"][0]["status"] == "Pending"   # the old version is still there to retry
        assert data["running"] == []


def test_validation_errors_are_still_returned_immediately_in_background_mode():
    app, _ = make_bg()
    with TestClient(app) as client:
        assert client.post("/artifacts/999/approve").status_code == 404
        rid = client.post("/requirements", json={"text": LONG_REQ}).json()["id"]
        data = wait_for(client, rid, lambda d: d["artifacts"] and idle(d))
        aid = data["awaiting"][0]["artifact_id"]
        assert client.post(f"/artifacts/{aid}/feedback", json={"feedback": "string"}).status_code == 422
        assert client.post(f"/artifacts/{aid}/reply",
                           json={"answers": [{"question": "Q?", "answer": "A"}]}).status_code == 422


# ---------------- Ollama limits ----------------
def test_ollama_client_sets_a_timeout_and_an_output_limit(monkeypatch):
    import aisew.llm.ollama_client as mod

    seen = {}

    class FakeAsyncClient:
        def __init__(self, **kw):
            seen["init"] = kw

        async def chat(self, **kw):
            seen["chat"] = kw
            return {"message": {"content": "{}"}, "prompt_eval_count": 3, "eval_count": 4}

    monkeypatch.setattr(mod.ollama, "AsyncClient", FakeAsyncClient)
    client = mod.OllamaClient(Settings(max_tokens=123, timeout_seconds=45.0, num_ctx=4096))
    result = asyncio.run(client.generate(system="s", user="u", schema={}))
    assert seen["init"]["timeout"] == 45.0
    assert seen["chat"]["options"]["num_predict"] == 123
    assert seen["chat"]["options"]["num_ctx"] == 4096
    assert result.completion_tokens == 4


def test_settings_read_the_new_environment_variables(monkeypatch):
    monkeypatch.setenv("AISEW_BACKGROUND", "0")
    monkeypatch.setenv("AISEW_MAX_TOKENS", "999")
    monkeypatch.setenv("AISEW_TIMEOUT", "12.5")
    s = Settings.from_env()
    assert s.background is False and s.max_tokens == 999 and s.timeout_seconds == 12.5
    monkeypatch.delenv("AISEW_BACKGROUND")
    assert Settings.from_env().background is True


def test_every_endpoint_that_touches_the_database_runs_on_the_event_loop_thread():
    """A plain `def` endpoint runs in another thread and races with the agents (lost writes,
    'dictionary changed size during iteration'). Keep them all `async def`."""
    import inspect

    app = create_app(llm=FakeLLM([]), database_url="sqlite://", sandbox=None)
    checked = [r for r in app.routes if getattr(r, "path", "").startswith(("/requirements", "/artifacts", "/projects", "/sandbox"))]
    assert len(checked) >= 10
    for route in checked:
        assert inspect.iscoroutinefunction(route.endpoint), f"{route.path} must be async def"
