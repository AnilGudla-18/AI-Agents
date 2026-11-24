"""The test-and-fix loop: run the generated tests in the sandbox, repair failing files, retry."""
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from aisew.agents.developer import DeveloperAgent, _files_in_traceback
from aisew.api.app import create_app
from aisew.domain.outputs import DeveloperOutput
from aisew.llm.fake import FakeLLM
from aisew.orchestration.pipeline import PipelineService
from aisew.sandbox.base import SandboxResult
from aisew.sandbox.docker import DockerSandbox

from test_pipeline_api import (
    DEV, LONG_REQ, MODELS_TEXT, REQS_TEXT, TESTS_TEXT, approve, approve_planning, calls_for,
    developer_plan, developer_replies, ids, routes, submit,
)
from test_sandbox import FakeDocker

FIXED_MODELS = MODELS_TEXT + "\n# fixed\n"
FAIL = SandboxResult(ran=True, passed=False, output=(
    "app/models.py:27: in <module>\n    amount = Column(Numeric)\nE   TypeError: bad argument\n1 failed"))
PASS = SandboxResult(ran=True, passed=True, output="3 passed in 0.2s")


class FakeSandbox:
    """Returns the scripted results in order and remembers the files it was given."""

    def __init__(self, *results):
        self.results = list(results)
        self.runs: list[list[tuple[str, str]]] = []

    async def run_tests(self, files):
        self.runs.append(list(files))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    async def status(self):
        return {"mode": "fake"}


def make(sandbox, extra_dev=(), monkeypatch=None, rounds=2):
    monkeypatch.setenv("AISEW_FIX_ROUNDS", str(rounds))
    llm = FakeLLM(routes=routes(**{DEV: developer_replies() + list(extra_dev)}))
    workspace = tempfile.mkdtemp()
    client = TestClient(create_app(llm=llm, database_url="sqlite://", workspace_dir=workspace,
                                   background=False, sandbox=sandbox))
    client.workspace = workspace
    return client, llm


def developer(r):
    return next(a for a in r["artifacts"] if a["agent"] == "Developer" and a["status"] == "Pending")


def awaiting_dev(r):
    return next(a for a in r["awaiting"] if a["agent"] == "Developer")


def events(r):
    return [e["event"] for e in r["audit_log"]]


# ---------------- the loop ----------------
def test_tests_pass_first_time(monkeypatch):
    sb = FakeSandbox(PASS)
    client, llm = make(sb, monkeypatch=monkeypatch)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["ran"] and report["passed"] and report["rounds"] == 0
    assert awaiting_dev(r)["action"] == "approve" and "DeveloperTestsPassed" in events(r)
    assert len(sb.runs) == 1 and dict(sb.runs[0])["app/models.py"] == MODELS_TEXT.rstrip("\n")
    assert len(calls_for(llm, DEV)) == 4  # plan + 3 files, no repair


def test_nothing_is_written_to_the_workspace_while_testing(monkeypatch):
    client, _ = make(FakeSandbox(PASS), monkeypatch=monkeypatch)
    r = approve_planning(client)
    assert r["workspace"] is None and list(Path(client.workspace).iterdir()) == []


def test_a_failing_file_is_repaired_and_the_tests_run_again(monkeypatch):
    sb = FakeSandbox(FAIL, PASS)
    client, llm = make(sb, extra_dev=[FIXED_MODELS], monkeypatch=monkeypatch)
    r = approve_planning(client)
    content = developer(r)["content"]
    assert content["test_report"]["passed"] and content["test_report"]["rounds"] == 1
    files = {f["path"]: f["content"] for f in content["files"]}
    assert files["app/models.py"] == FIXED_MODELS.rstrip("\n")       # the failing file was rewritten
    assert files["tests/test_cancel.py"] == TESTS_TEXT.rstrip("\n")  # the others were left alone
    assert dict(sb.runs[1])["app/models.py"] == FIXED_MODELS.rstrip("\n")  # the 2nd run tested the fix
    repair = calls_for(llm, DEV)[4]
    assert repair["schema"] is None and "tests were run and FAILED" in repair["user"]
    assert "TypeError: bad argument" in repair["user"] and "Your previous version of this file" in repair["user"]
    assert len(calls_for(llm, DEV)) == 5  # exactly one extra call: only app/models.py was rewritten
    assert awaiting_dev(r)["action"] == "approve" and "DeveloperTestsPassed" in events(r)


def test_repair_usage_is_recorded_and_the_loop_stops_after_the_limit(monkeypatch):
    sb = FakeSandbox(FAIL)  # always fails
    client, llm = make(sb, extra_dev=[FIXED_MODELS, FIXED_MODELS], monkeypatch=monkeypatch, rounds=2)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["ran"] and report["passed"] is False and report["rounds"] == 2
    assert "TypeError: bad argument" in report["summary"]
    assert len(sb.runs) == 3 and len(calls_for(llm, DEV)) == 6   # 2 fix rounds, then it gives up
    dev = awaiting_dev(r)
    assert dev["action"] == "review"
    assert any("still fail after 2 automatic fix rounds" in i for i in dev["quality_issues"])
    assert developer(r)["confidence"] <= 60
    assert "DeveloperTestsFailed" in events(r)


def test_a_failed_loop_can_still_be_approved_and_written(monkeypatch):
    client, _ = make(FakeSandbox(FAIL), extra_dev=[FIXED_MODELS] * 2, monkeypatch=monkeypatch)
    r = approve_planning(client)
    r = approve(client, awaiting_dev(r)["artifact_id"])
    assert r["status"] == "Complete" and (Path(r["workspace"]) / "app" / "models.py").exists()


def test_tests_that_could_not_run_are_reported_but_do_not_block(monkeypatch):
    nope = SandboxResult(ran=False, reason="Docker is not installed or not running, so the tests were not run.")
    sb = FakeSandbox(nope)
    client, llm = make(sb, monkeypatch=monkeypatch)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["ran"] is False and "Docker is not installed" in report["summary"]
    assert awaiting_dev(r)["action"] == "approve"
    assert any(x.startswith("Tests were not run") for x in developer(r)["content"]["risks"])
    assert "DeveloperTestsNotRun" in events(r) and len(calls_for(llm, DEV)) == 4


def test_a_switched_off_sandbox_is_reported(monkeypatch):
    client, _ = make(None, monkeypatch=monkeypatch)
    r = approve_planning(client)
    assert "switched off" in developer(r)["content"]["test_report"]["summary"]


def test_a_developer_with_a_blocking_question_is_not_tested(monkeypatch):
    q = [{"question": "Card or wallet?", "blocking": True}]
    sb = FakeSandbox(PASS)
    llm = FakeLLM(routes=routes(**{DEV: [developer_plan(open_questions=q)]}))
    client = TestClient(create_app(llm=llm, database_url="sqlite://", workspace_dir=tempfile.mkdtemp(),
                                   background=False, sandbox=sb))
    r = approve_planning(client)
    assert sb.runs == [] and awaiting_dev(r)["action"] == "reply"


def test_the_loop_also_works_in_background_mode(monkeypatch):
    monkeypatch.setenv("AISEW_FIX_ROUNDS", "2")
    sb = FakeSandbox(FAIL, PASS)
    llm = FakeLLM(routes=routes(**{DEV: developer_replies() + [FIXED_MODELS]}))
    app = create_app(llm=llm, database_url="sqlite://", workspace_dir=tempfile.mkdtemp(),
                     background=True, sandbox=sb)

    def wait(client, cond):
        end = time.time() + 45
        while time.time() < end:
            d = client.get("/requirements/1").json()
            if cond(d):
                return d
            time.sleep(0.03)
        raise AssertionError("timed out")

    with TestClient(app) as client:
        client.post("/requirements", json={"text": LONG_REQ})
        d = wait(client, lambda d: d["artifacts"] and not d["running"])
        client.post(f"/artifacts/{ids(d)['BA']}/approve")
        d = wait(client, lambda d: len(d["artifacts"]) == 4 and not d["running"])
        for agent in ("DBA", "Manager", "QA"):
            client.post(f"/artifacts/{ids(d)[agent]}/approve")
        d = wait(client, lambda d: "Developer" in ids(d) and not d["running"])
        assert developer(d)["content"]["test_report"]["rounds"] == 1


# ---------------- reading the traceback ----------------
KNOWN = ["app/models.py", "app/refund.py", "tests/test_refund.py"]
TB = """tests/test_refund.py:12: in test_a
    service.cancel(1)
app/refund.py:20: in cancel
    raise ValueError('x')
E   ValueError: x
"""


def test_traceback_files_deepest_frame_first():
    assert _files_in_traceback(TB, KNOWN) == ["app/refund.py", "tests/test_refund.py"]


def test_traceback_formats_from_a_container_and_windows():
    assert _files_in_traceback('File "/work/app/models.py", line 27\n  x = (', KNOWN) == ["app/models.py"]
    assert _files_in_traceback("C:\\work\\app\\models.py:3: in <module>", KNOWN) == ["app/models.py"]
    assert _files_in_traceback('File "/usr/lib/python3.11/site-packages/sqlalchemy/orm.py", line 4', KNOWN) == []


async def _repair(test_output, files):
    out = DeveloperOutput.model_validate({"readiness": "Ready", "confidence": 80, "files": files})
    ctx = {"requirement": "r", "ba_output": {}, "dba_output": {"tables": []}, "manager_output": {}, "qa_output": {}}
    llm = FakeLLM(["x = 1\n"] * 5)
    new, usage = await DeveloperAgent(llm).repair(ctx, out, test_output)
    return new, usage, llm


async def test_repair_rewrites_at_most_three_files_named_in_the_traceback():
    files = [{"path": p, "content": "old = 1\n", "purpose": "p"} for p in
             ["app/a.py", "app/b.py", "app/c.py", "app/d.py", "tests/test_a.py"]]
    tb = "tests/test_a.py:1: in t\napp/a.py:1: in f\napp/b.py:1: in g\napp/c.py:1: in h\napp/d.py:1: in i\n"
    new, usage, llm = await _repair(tb, files)
    changed = [f.path for f, o in zip(new.files, files) if f.content != o["content"]]
    assert changed == ["app/b.py", "app/c.py", "app/d.py"] and len(llm.calls) == 3


async def test_repair_falls_back_to_the_test_file_when_no_file_is_named():
    files = [{"path": "app/a.py", "content": "old = 1\n", "purpose": "p"},
             {"path": "tests/test_a.py", "content": "old = 1\n", "purpose": "p"}]
    new, _, llm = await _repair("something unrelated went wrong", files)
    assert [f.content for f in new.files] == ["old = 1\n", "x = 1"] and len(llm.calls) == 1


async def test_repair_keeps_everything_else_about_the_artifact():
    files = [{"path": "app/a.py", "content": "old = 1\n", "purpose": "p"}]
    out = DeveloperOutput.model_validate({"readiness": "Ready", "confidence": 77, "summary": "s",
                                          "risks": ["kept"], "files": files, "run_instructions": "run it"})
    ctx = {"requirement": "r", "ba_output": {}, "dba_output": {"tables": []}, "manager_output": {}, "qa_output": {}}
    new, _ = await DeveloperAgent(FakeLLM(["y = 2\n"])).repair(ctx, out, "app/a.py:1: boom")
    assert new.confidence == 77 and new.risks == ["kept"] and new.run_instructions == "run it"


def test_first_error_line_for_the_summary():
    assert PipelineService._first_error("a\nE   KeyError: 1\nE   ValueError: boom\n1 failed") == "E   ValueError: boom"
    assert PipelineService._first_error("just a line") == "just a line"
    assert PipelineService._first_error("") == "tests failed"


# ---------------- the /sandbox endpoints ----------------
def test_sandbox_endpoints():
    off = TestClient(create_app(llm=FakeLLM([]), database_url="sqlite://", sandbox=None))
    assert off.get("/sandbox").json() == {"mode": "off"}
    assert off.post("/sandbox/build").json() == {"mode": "off"}

    fake = FakeDocker({"image": (1, "")})
    sb = DockerSandbox(timeout=5, run=fake, which=lambda n: "docker")
    with TestClient(create_app(llm=FakeLLM([]), database_url="sqlite://", sandbox=sb)) as client:
        assert client.get("/sandbox").json()["image_ready"] is False
        started = client.post("/sandbox/build").json()
        assert started["building"] is True
        fake.script["image"] = (0, "")   # the build finished: the image now exists
        end = time.time() + 5
        while time.time() < end and client.get("/sandbox").json()["building"]:
            time.sleep(0.02)
        status = client.get("/sandbox").json()
        assert status["image_ready"] is True and status["docker_available"] is True
        assert any(c[1] == "build" for c in fake.calls)


# ---------------- rule-based fixes run before the model is asked to rewrite anything ----------------
FLOAT_MODELS = MODELS_TEXT + '\n\nclass Price(Base):\n    __tablename__ = "prices"\n    amount = Float\n'
NAME_ERROR = SandboxResult(ran=True, passed=False, output=(
    "app/models.py:25: in <module>\n    amount = Float\nE   NameError: name 'Float' is not defined\n1 failed"))
OTHER_ERROR = SandboxResult(ran=True, passed=False, output=(
    "app/models.py:3: in <module>\nE   TypeError: something the rules cannot fix\n1 failed"))


def make_with(sandbox, dev_replies, monkeypatch, rounds=2, background=False):
    monkeypatch.setenv("AISEW_FIX_ROUNDS", str(rounds))
    llm = FakeLLM(routes=routes(**{DEV: dev_replies}))
    workspace = tempfile.mkdtemp()
    client = TestClient(create_app(llm=llm, database_url="sqlite://", workspace_dir=workspace,
                                   background=background, sandbox=sandbox))
    client.workspace = workspace
    return client, llm


def test_a_missing_import_is_fixed_by_rule_without_asking_the_model(monkeypatch):
    sb = FakeSandbox(NAME_ERROR, PASS)
    client, llm = make_with(sb, [developer_plan(), FLOAT_MODELS, TESTS_TEXT, REQS_TEXT], monkeypatch)
    r = approve_planning(client)
    content = developer(r)["content"]
    report = content["test_report"]
    assert report["passed"] and report["rounds"] == 0                       # no model fix round was needed
    assert report["auto_fixes"] == ["app/models.py: from sqlalchemy import Float"]
    assert len(calls_for(llm, DEV)) == 4                                    # plan + 3 files, no repair call
    models = next(f["content"] for f in content["files"] if f["path"] == "app/models.py")
    assert models.splitlines()[0] == "from sqlalchemy import Float"
    assert dict(sb.runs[1])["app/models.py"].startswith("from sqlalchemy import Float")  # the fix was what got re-tested
    assert awaiting_dev(r)["action"] == "approve"
    assert "DeveloperAutoFix" in events(r) and "DeveloperTestsPassed" in events(r)


def test_rule_fixes_do_not_use_up_the_model_fix_rounds(monkeypatch):
    sb = FakeSandbox(NAME_ERROR, OTHER_ERROR, PASS)
    client, llm = make_with(sb, [developer_plan(), FLOAT_MODELS, TESTS_TEXT, REQS_TEXT, FIXED_MODELS],
                            monkeypatch, rounds=1)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["passed"] and report["rounds"] == 1 and len(report["auto_fixes"]) == 1
    assert len(calls_for(llm, DEV)) == 5 and len(sb.runs) == 3


def test_a_name_the_rules_already_fixed_cannot_loop_forever(monkeypatch):
    already = "from sqlalchemy import Float\n" + FLOAT_MODELS
    sb = FakeSandbox(NAME_ERROR)  # keeps failing with the same NameError
    client, llm = make_with(sb, [developer_plan(), already, TESTS_TEXT, REQS_TEXT, FIXED_MODELS],
                            monkeypatch, rounds=1)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["passed"] is False and report["rounds"] == 1 and report["auto_fixes"] == []
    assert len(sb.runs) == 2                      # the rules found nothing to fix, so the model got its one round
    assert awaiting_dev(r)["action"] == "review"


# ---------------- retest: the stored code's tests run again, no model rewrite ----------------
def failed_developer(monkeypatch, background=False):
    """A pending Developer artifact generated while Docker was missing (so it was never tested)."""
    sb = FakeSandbox(SandboxResult(ran=False, reason="Docker is not installed or not running, so the tests were not run."))
    client, llm = make_with(sb, [developer_plan(), FLOAT_MODELS, TESTS_TEXT, REQS_TEXT], monkeypatch,
                            background=background)
    return client, llm, sb


def test_retest_fixes_a_stored_artifact_without_any_model_call(monkeypatch):
    client, llm, sb = failed_developer(monkeypatch)
    r = approve_planning(client)
    first = awaiting_dev(r)["artifact_id"]
    assert developer(r)["content"]["test_report"]["ran"] is False
    calls_before = len(llm.calls)

    sb.results = [NAME_ERROR, PASS]   # Docker is available now
    r = client.post(f"/artifacts/{first}/retest").json()
    devs = [a for a in r["artifacts"] if a["agent"] == "Developer"]
    assert [(a["version"], a["status"]) for a in devs] == [(1, "Superseded"), (2, "Pending")]
    new = devs[1]["content"]
    assert new["test_report"]["passed"] and new["test_report"]["auto_fixes"] == ["app/models.py: from sqlalchemy import Float"]
    assert not any(x.startswith("Tests were not run") for x in new["risks"])   # the old note is gone
    assert len(llm.calls) == calls_before                                       # the model was never asked
    assert "DeveloperRetested" in events(r) and "DeveloperAutoFix" in events(r)
    assert awaiting_dev(r)["action"] == "approve"

    r = approve(client, awaiting_dev(r)["artifact_id"])   # and the fixed code is what gets saved
    assert r["status"] == "Complete"
    saved = (Path(r["workspace"]) / "app" / "models.py").read_text().splitlines()[0]
    assert saved == "from sqlalchemy import Float"


def test_retest_that_still_fails_asks_the_model_to_fix_the_named_file(monkeypatch):
    client, llm, sb = failed_developer(monkeypatch)
    r = approve_planning(client)
    first = awaiting_dev(r)["artifact_id"]
    llm._routes[DEV].append(FIXED_MODELS)   # what the model will answer for the repair
    sb.results = [OTHER_ERROR, PASS]
    r = client.post(f"/artifacts/{first}/retest").json()
    new = [a for a in r["artifacts"] if a["agent"] == "Developer"][-1]["content"]
    assert new["test_report"]["passed"] and new["test_report"]["rounds"] == 1
    assert next(f["content"] for f in new["files"] if f["path"] == "app/models.py").endswith("# fixed")


def test_retest_guard_rails(monkeypatch):
    client, _, sb = failed_developer(monkeypatch)
    r = approve_planning(client)
    dev_id = awaiting_dev(r)["artifact_id"]
    assert client.post("/artifacts/999/retest").status_code == 404
    assert client.post(f"/artifacts/{ids(r)['QA']}/retest").status_code == 422     # not a Developer artifact
    sb.results = [PASS]
    approved = approve(client, dev_id)
    assert client.post(f"/artifacts/{dev_id}/retest").status_code == 409            # already approved
    assert approved["status"] == "Complete"


def test_retest_works_in_the_background_too(monkeypatch):
    client, llm, sb = failed_developer(monkeypatch, background=True)

    def wait(cond):
        end = time.time() + 45
        while time.time() < end:
            d = client.get("/requirements/1").json()
            if cond(d):
                return d
            time.sleep(0.03)
        raise AssertionError("timed out")

    with client:
        client.post("/requirements", json={"text": LONG_REQ})
        d = wait(lambda d: d["artifacts"] and not d["running"])
        client.post(f"/artifacts/{ids(d)['BA']}/approve")
        d = wait(lambda d: len(d["artifacts"]) == 4 and not d["running"])
        for agent in ("DBA", "Manager", "QA"):
            client.post(f"/artifacts/{ids(d)[agent]}/approve")
        d = wait(lambda d: "Developer" in ids(d) and not d["running"])
        sb.results = [NAME_ERROR, PASS]
        started = client.post(f"/artifacts/{awaiting_dev(d)['artifact_id']}/retest").json()
        assert started["running"] and started["running"][0]["agent"] == "Developer"
        d = wait(lambda d: any(a["version"] == 2 and a["agent"] == "Developer" for a in d["artifacts"]) and not d["running"])
        assert developer(d)["content"]["test_report"]["passed"]


# ---------------- files that disagree about a name (the password-reset run) ----------------
EMAIL_TEXT = "def send_reset_email(to, link):\n    return True\n"
RESET_TESTS = "from app.email_service import send_email\n\n\ndef test_reset():\n    assert send_email('a@b.c', 'x')\n"
RESET_PLAN = developer_plan(files=[
    {"path": "app/models.py", "purpose": "models for bookings and refunds"},
    {"path": "app/email_service.py", "purpose": "function send_reset_email"},
    {"path": "tests/test_reset.py", "purpose": "tests for the reset email"},
    {"path": "requirements.txt", "purpose": "packages"},
])
RESET_REPLIES = [RESET_PLAN, MODELS_TEXT, EMAIL_TEXT, RESET_TESTS, REQS_TEXT]
IMPORT_ERROR = SandboxResult(ran=True, passed=False, output=(
    "tests/test_reset.py:1: in <module>\n    from app.email_service import send_email\n"
    "E   ImportError: cannot import name 'send_email' from 'app.email_service' (/work/app/email_service.py)\n1 error"))


def test_a_name_mismatch_between_files_is_fixed_by_rule_during_generation(monkeypatch):
    sb = FakeSandbox(IMPORT_ERROR, PASS)
    client, llm = make_with(sb, RESET_REPLIES, monkeypatch)
    r = approve_planning(client)
    report = developer(r)["content"]["test_report"]
    assert report["passed"] and report["rounds"] == 0
    assert report["auto_fixes"] == ["app/email_service.py: send_email = send_reset_email (alias: other files import send_email)"]
    assert len(calls_for(llm, DEV)) == 5                                   # plan + 4 files: the model never repaired
    email = next(f["content"] for f in developer(r)["content"]["files"] if f["path"] == "app/email_service.py")
    assert email.strip().endswith("send_email = send_reset_email")
    assert dict(sb.runs[1])["app/email_service.py"].strip().endswith("send_email = send_reset_email")


def test_retest_repairs_a_stored_artifact_whose_files_disagree(monkeypatch):
    """The exact situation: the Developer result is pending, its tests fail on an import mismatch."""
    sb = FakeSandbox(SandboxResult(ran=False, reason="Docker is not installed or not running, so the tests were not run."))
    client, llm = make_with(sb, RESET_REPLIES, monkeypatch)
    r = approve_planning(client)
    stored = awaiting_dev(r)["artifact_id"]
    calls = len(llm.calls)

    sb.results = [IMPORT_ERROR, PASS]
    r = client.post(f"/artifacts/{stored}/retest").json()
    new = developer(r)["content"]
    assert new["test_report"]["passed"] and len(new["test_report"]["auto_fixes"]) == 1
    assert len(llm.calls) == calls                                          # fixed with no model call at all
    assert awaiting_dev(r)["action"] == "approve"
    r = approve(client, awaiting_dev(r)["artifact_id"])
    saved = (Path(r["workspace"]) / "app" / "email_service.py").read_text()
    assert "send_email = send_reset_email" in saved
