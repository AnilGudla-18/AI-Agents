import asyncio
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from aisew.api.app import create_app
from aisew.llm.fake import FakeLLM

LONG_REQ = ("Customers must be able to cancel an existing booking from their account page "
            "and the system must then return the money they paid for that booking")

BA, DBA, MGR, QA, DEV = (
    "Business Analyst", "Database Architect", "Delivery Manager", "QA Engineer", "Software Developer",
)


def ba_reply(**o) -> str:
    d = {"readiness": "Ready", "confidence": 90, "summary": "cancel and refund", "open_questions": [],
         "risks": [], "user_stories": [{"as_a": "customer", "i_want": "cancel", "so_that": "refund"}],
         "business_rules": ["A cancelled booking is refunded"],
         "acceptance_criteria": ["Given a booking, when cancelled, then a refund is issued"]}
    d.update(o)
    return json.dumps(d)


def dba_reply(**o) -> str:
    d = {"readiness": "Ready", "confidence": 85, "summary": "schema",
         "tables": [
             {"name": "bookings", "purpose": "bookings", "columns": [
                 {"name": "id", "type": "uuid", "nullable": False, "primary_key": True},
                 {"name": "status", "type": "text"},
                 {"name": "created_at", "type": "timestamp"}]},
             {"name": "refunds", "purpose": "refunds", "columns": [
                 {"name": "id", "type": "uuid", "nullable": False, "primary_key": True},
                 {"name": "booking_id", "type": "uuid", "references": "bookings.id"}]}],
         "relationships": [], "indexes": []}
    d.update(o)
    return json.dumps(d)


def manager_reply(**o) -> str:
    d = {"readiness": "Ready", "confidence": 80, "summary": "plan", "open_questions": [], "risks": [],
         "tasks": [{"title": "Build cancel endpoint", "role": "backend developer", "effort": "M", "depends_on": []},
                   {"title": "Add cancel button", "role": "frontend developer", "effort": "S",
                    "depends_on": ["Build cancel endpoint"]}],
         "milestones": ["Cancellation works end to end"]}
    d.update(o)
    return json.dumps(d)


def qa_reply(**o) -> str:
    d = {"readiness": "Ready", "confidence": 80, "summary": "tests", "open_questions": [], "risks": [],
         "test_cases": [
             {"title": "Cancel a booking", "kind": "functional", "covers": [1],
              "steps": ["Open a booking", "Click cancel"], "expected": "The booking is cancelled and refunded"},
             {"title": "Cancel twice", "kind": "negative", "covers": [],
              "steps": ["Open a cancelled booking", "Try to cancel again"], "expected": "The request is refused"}]}
    d.update(o)
    return json.dumps(d)


MODELS_TEXT = ("from sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n\n\n"
               "class Booking(Base):\n    __tablename__ = \"bookings\"\n\n\n"
               "class Refund(Base):\n    __tablename__ = \"refunds\"\n")
TESTS_TEXT = "def test_cancel():\n    assert True\n"
REQS_TEXT = "sqlalchemy\npytest\n"
GOOD_PLAN_FILES = [
    {"path": "app/models.py", "purpose": "models for bookings and refunds"},
    {"path": "tests/test_cancel.py", "purpose": "tests for cancelling"},
    {"path": "requirements.txt", "purpose": "packages"},
]


def developer_plan(**o) -> str:
    d = {"readiness": "Ready", "confidence": 80, "summary": "backend", "open_questions": [], "risks": [],
         "files": GOOD_PLAN_FILES, "run_instructions": "pip install -r requirements.txt"}
    d.update(o)
    return json.dumps(d)


def developer_replies(plan=None):
    """What the fake Developer says, in order: the plan, then the text of each file."""
    return [plan or developer_plan(), MODELS_TEXT, TESTS_TEXT, REQS_TEXT]


def routes(**overrides):
    r = {BA: [ba_reply()], DBA: [dba_reply()], MGR: [manager_reply()], QA: [qa_reply()],
         DEV: developer_replies()}
    r.update(overrides)
    return r


def make(**overrides):
    llm = FakeLLM(routes=routes(**overrides))
    workspace = tempfile.mkdtemp()
    client = TestClient(create_app(llm=llm, database_url="sqlite://", workspace_dir=workspace, background=False, sandbox=None))
    client.workspace = workspace
    return client, llm


def submit(client):
    r = client.post("/requirements", json={"text": LONG_REQ})
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, artifact_id):
    r = client.post(f"/artifacts/{artifact_id}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def ids(data):
    return {a["agent"]: a["id"] for a in data["artifacts"]}


def calls_for(llm, agent):
    return [c for c in llm.calls if c["agent"] == agent]


# ---------------- ordering and dependencies ----------------
def test_create_requirement_runs_only_ba():
    client, _ = make()
    data = submit(client)
    assert [a["agent"] for a in data["artifacts"]] == ["BA"]
    assert data["status"] == "InReview"
    assert data["awaiting"] == [
        {"artifact_id": 1, "agent": "BA", "readiness": "Ready", "action": "approve", "quality_issues": []}
    ]


def test_approving_ba_runs_dba_manager_and_qa():
    client, _ = make()
    r = approve(client, ids(submit(client))["BA"])
    assert set(ids(r)) == {"BA", "DBA", "Manager", "QA"}
    assert [a["agent"] for a in r["awaiting"]] == ["DBA", "Manager", "QA"]
    assert r["needs_retry"] == []


def test_downstream_prompts_contain_the_approved_analysis():
    client, llm = make()
    approve(client, ids(submit(client))["BA"])
    assert "then a refund is issued" in calls_for(llm, DBA)[0]["user"]
    assert "AC1: Given a booking" in calls_for(llm, MGR)[0]["user"]
    assert "AC1: Given a booking" in calls_for(llm, QA)[0]["user"]


def approve_planning(client):
    """Approve BA, then DBA + Manager + QA. Returns the last response."""
    r = approve(client, ids(submit(client))["BA"])
    by = ids(r)
    for agent in ("DBA", "Manager", "QA"):
        r = approve(client, by[agent])
    return r


def test_developer_waits_for_dba_manager_and_qa():
    client, _ = make()
    r = approve(client, ids(submit(client))["BA"])
    by = ids(r)
    r = approve(client, by["DBA"])
    r = approve(client, by["Manager"])
    assert "Developer" not in ids(r) and r["status"] == "InReview"
    r = approve(client, by["QA"])
    assert "Developer" in ids(r)
    assert [a["agent"] for a in r["awaiting"]] == ["Developer"]


def test_complete_only_after_the_developer_is_approved_and_files_are_written():
    client, _ = make()
    r = approve_planning(client)
    assert r["status"] == "InReview" and r["workspace"] is None
    r = approve(client, ids(r)["Developer"])
    assert r["status"] == "Complete" and r["awaiting"] == []
    root = Path(r["workspace"])
    assert root.is_dir() and str(root).startswith(str(Path(client.workspace).resolve()))
    assert "__tablename__" in (root / "app" / "models.py").read_text()
    assert (root / "tests" / "test_cancel.py").exists() and (root / "requirements.txt").exists()
    events = [e["event"] for e in r["audit_log"]]
    assert "DeveloperFilesWritten" in events and "DeveloperFileSkipped" not in events


def test_nothing_is_written_before_approval():
    client, _ = make()
    r = approve_planning(client)
    assert r["workspace"] is None
    assert list(Path(client.workspace).iterdir()) == []


def test_unsafe_paths_are_skipped_and_never_written_outside_the_workspace():
    evil = GOOD_PLAN_FILES + [
        {"path": "../evil.py", "purpose": "bad"},
        {"path": "C:\\Windows\\evil.py", "purpose": "bad"},
        {"path": "/etc/evil.py", "purpose": "bad"},
        {"path": "app/run.exe", "purpose": "bad"},
    ]
    bad_plan = developer_plan(files=evil)
    client, llm = make(**{DEV: [bad_plan, bad_plan, MODELS_TEXT, TESTS_TEXT, REQS_TEXT]})
    r = approve_planning(client)
    dev = next(a for a in r["awaiting"] if a["agent"] == "Developer")
    assert dev["action"] == "review"
    assert any("Unsafe or invalid path" in i for i in dev["quality_issues"])
    assert len([c for c in calls_for(llm, DEV) if c["schema"] is None]) == 3  # no code for unsafe paths

    r = approve(client, dev["artifact_id"])  # approving anyway must stay safe
    root = Path(r["workspace"])
    assert (root / "app" / "models.py").exists()
    assert not (root.parent / "evil.py").exists() and not (root / "evil.py").exists()
    skipped = [e["detail"] for e in r["audit_log"] if e["event"] == "DeveloperFileSkipped"]
    assert len(skipped) == 4 and any("evil.py" in s for s in skipped)
    assert not list(root.rglob("*.exe"))


def test_developer_prompt_contains_every_approved_upstream_output():
    client, llm = make()
    approve_planning(client)
    user = calls_for(llm, DEV)[0]["user"]
    assert "AC1: Given a booking" in user                      # BA
    assert "- bookings:" in user and "FK->bookings.id" in user  # DBA
    assert "1. Build cancel endpoint" in user                  # Manager
    assert "Cancel a booking" in user                          # QA


def test_code_endpoint_returns_readable_text():
    client, _ = make()
    r = approve_planning(client)
    dev_id = ids(r)["Developer"]
    text = client.get(f"/artifacts/{dev_id}/code")
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
    assert "===== app/models.py =====" in text.text and '__tablename__ = "bookings"' in text.text
    assert client.get(f"/artifacts/{ids(r)['QA']}/code").status_code == 422
    assert client.get("/artifacts/999/code").status_code == 404


def test_audit_log_records_events():
    client, _ = make()
    r = approve(client, ids(submit(client))["BA"])
    events = [e["event"] for e in r["audit_log"]]
    assert events[:3] == ["RequirementCreated", "BAGenerated", "BAApproved"]
    assert set(events[3:]) == {"DBAGenerated", "ManagerGenerated", "QAGenerated"}


# ---------------- parallelism ----------------
class SlowFake(FakeLLM):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.in_flight = 0
        self.max_in_flight = 0

    async def generate(self, **kw):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.05)
        try:
            return await super().generate(**kw)
        finally:
            self.in_flight -= 1


def _slow_run(max_parallel):
    llm = SlowFake(routes=routes())
    client = TestClient(create_app(llm=llm, database_url="sqlite://", max_parallel=max_parallel, background=False, sandbox=None))
    approve(client, ids(submit(client))["BA"])
    return llm


def test_unblocked_stages_run_in_parallel():
    assert _slow_run(3).max_in_flight == 3


def test_max_parallel_limits_concurrency():
    assert _slow_run(1).max_in_flight == 1


# ---------------- failures ----------------
def test_one_failed_stage_does_not_block_the_others_and_can_be_retried():
    client, _ = make(**{QA: ["garbage", "garbage", qa_reply()]})
    r = approve(client, ids(submit(client))["BA"])
    assert set(ids(r)) == {"BA", "DBA", "Manager"}
    assert r["needs_retry"] == ["QA"]
    assert any(e["event"] == "QAFailed" for e in r["audit_log"])

    r = client.post(f"/requirements/{r['id']}/run").json()
    assert set(ids(r)) == {"BA", "DBA", "Manager", "QA"}
    assert r["needs_retry"] == []


def test_failed_first_stage_is_visible_and_retryable():
    client, _ = make(**{BA: ["garbage", "garbage", ba_reply()]})
    data = submit(client)
    assert data["artifacts"] == []
    assert data["needs_retry"] == ["BA"]
    assert data["status"] == "NeedsAttention"
    assert any(e["event"] == "BAFailed" for e in data["audit_log"])
    data = client.post(f"/requirements/{data['id']}/run").json()
    assert [a["agent"] for a in data["artifacts"]] == ["BA"]


# ---------------- approval + clarification ----------------
QS = [{"question": "Which gateway?", "blocking": True}]


def test_cannot_approve_not_ready_and_action_is_reply():
    client, _ = make(**{BA: [ba_reply(open_questions=QS)]})
    data = submit(client)
    assert data["artifacts"][0]["readiness"] == "NotReady"
    assert data["awaiting"][0]["action"] == "reply"
    assert client.post(f"/artifacts/{data['artifacts'][0]['id']}/approve").status_code == 409


def test_reply_reruns_agent_and_supersedes_old_version():
    client, llm = make(**{BA: [ba_reply(open_questions=QS), ba_reply()]})
    data = submit(client)
    r = client.post(f"/artifacts/{data['artifacts'][0]['id']}/reply",
                    json={"answers": [{"question": "Which gateway?", "answer": "Stripe"}]}).json()
    assert [(a["version"], a["status"]) for a in r["artifacts"]] == [(1, "Superseded"), (2, "Pending")]
    assert "A: Stripe" in calls_for(llm, BA)[1]["user"]
    assert r["clarifications"][0]["answer"] == "Stripe"


def test_reply_rejects_placeholder_answers_and_unasked_questions():
    client, _ = make(**{BA: [ba_reply(open_questions=QS), ba_reply()]})
    aid = submit(client)["artifacts"][0]["id"]
    url = f"/artifacts/{aid}/reply"

    r = client.post(url, json={"answers": [{"question": "Which gateway?", "answer": "string"}]})
    assert r.status_code == 422 and "placeholder" in r.json()["detail"]

    r = client.post(url, json={"answers": [{"question": "string", "answer": "string"}]})
    assert r.status_code == 422 and "did not ask" in r.json()["detail"]

    r = client.post(url, json={"answers": [{"question": "Which gateway?", "answer": "   "}]})
    assert r.status_code == 422

    # question text is matched ignoring case and spacing
    r = client.post(url, json={"answers": [{"question": "which  GATEWAY?", "answer": "Stripe"}]})
    assert r.status_code == 200
    assert r.json()["clarifications"][0]["question"] == "Which gateway?"


def test_reply_to_artifact_without_questions_is_rejected():
    client, _ = make()
    aid = submit(client)["artifacts"][0]["id"]
    r = client.post(f"/artifacts/{aid}/reply", json={"answers": [{"question": "Q?", "answer": "A"}]})
    assert r.status_code == 422


def test_cannot_approve_superseded_artifact():
    qs = [{"question": "Q?", "blocking": True}]
    client, _ = make(**{BA: [ba_reply(open_questions=qs), ba_reply()]})
    old_id = submit(client)["artifacts"][0]["id"]
    client.post(f"/artifacts/{old_id}/reply", json={"answers": [{"question": "Q?", "answer": "A"}]})
    assert client.post(f"/artifacts/{old_id}/approve").status_code == 409


def test_unknown_ids_return_404():
    client, _ = make()
    assert client.get("/requirements/999").status_code == 404
    assert client.post("/artifacts/999/approve").status_code == 404
    assert client.post("/requirements/999/run").status_code == 404


# ---------------- feedback endpoint ----------------
def test_feedback_revises_an_artifact_that_has_no_open_questions():
    client, llm = make(**{BA: [ba_reply(), ba_reply(summary="Revised summary")]})
    aid = submit(client)["artifacts"][0]["id"]
    r = client.post(f"/artifacts/{aid}/feedback", json={"feedback": "Remove the refund rule please"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert [(a["version"], a["status"]) for a in data["artifacts"]] == [(1, "Superseded"), (2, "Pending")]
    assert data["artifacts"][1]["content"]["summary"] == "Revised summary"
    second = calls_for(llm, BA)[1]["user"]
    assert "Remove the refund rule please" in second
    assert "Your previous answer" in second and "cancel and refund" in second
    assert "BAFeedbackGiven" in [e["event"] for e in data["audit_log"]]


def test_feedback_is_rejected_for_placeholders_and_for_approved_artifacts():
    client, _ = make()
    aid = submit(client)["artifacts"][0]["id"]
    assert client.post(f"/artifacts/{aid}/feedback", json={"feedback": "string"}).status_code == 422
    assert client.post(f"/artifacts/{aid}/feedback", json={"feedback": "x"}).status_code == 422
    approve(client, aid)
    r = client.post(f"/artifacts/{aid}/feedback", json={"feedback": "Change something please"})
    assert r.status_code == 409
    assert client.post("/artifacts/999/feedback", json={"feedback": "Change something"}).status_code == 404


def test_feedback_persists_for_later_reruns_of_the_same_agent():
    qs = [{"question": "Which gateway?", "blocking": False}]
    client, llm = make(**{BA: [ba_reply(open_questions=qs), ba_reply(open_questions=qs), ba_reply()]})
    aid = submit(client)["artifacts"][0]["id"]
    r = client.post(f"/artifacts/{aid}/feedback", json={"feedback": "Keep rules short"}).json()
    new_id = r["awaiting"][0]["artifact_id"]
    client.post(f"/artifacts/{new_id}/reply", json={"answers": [{"question": "Which gateway?", "answer": "Stripe"}]})
    third = calls_for(llm, BA)[2]["user"]
    assert "Keep rules short" in third and "A: Stripe" in third


# ---------------- BA answers reach every downstream agent ----------------
def test_ba_answers_are_given_to_dba_manager_and_qa_as_confirmed_facts():
    qs = [{"question": "How is the refund paid?", "blocking": False}]
    client, llm = make(**{BA: [ba_reply(open_questions=qs), ba_reply()]})
    aid = submit(client)["artifacts"][0]["id"]
    r = client.post(f"/artifacts/{aid}/reply",
                    json={"answers": [{"question": "How is the refund paid?", "answer": "To the original card"}]}).json()
    approve(client, r["awaiting"][0]["artifact_id"])
    for agent in (DBA, MGR, QA):
        user = calls_for(llm, agent)[0]["user"]
        assert "Facts confirmed by the business" in user, agent
        assert "To the original card" in user, agent


# ---------------- review action when quality problems remain ----------------
def test_awaiting_action_is_review_when_quality_issues_remain():
    flawed = qa_reply(test_cases=[
        {"title": "Cancel", "kind": "functional", "covers": [1], "steps": ["x"], "expected": "y"}])
    client, _ = make(**{QA: [flawed, flawed]})
    r = approve(client, ids(submit(client))["BA"])
    qa = next(a for a in r["awaiting"] if a["agent"] == "QA")
    assert qa["action"] == "review"
    assert any("negative test" in i for i in qa["quality_issues"])
    assert next(a for a in r["awaiting"] if a["agent"] == "DBA")["action"] == "approve"
