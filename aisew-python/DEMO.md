# AISEW demo guide (5 minutes)

How to present the project, what to say, and what to expect when someone asks hard questions.

## The one-sentence pitch

> "I type a requirement in plain English. A team of AI agents analyses it, designs the database, plans the work
> and writes the tests, then a developer agent writes the code, which is tested inside a locked-down Docker
> container. I approve every stage, and everything is recorded in an audit log."

## Before you present (10 minutes, do not skip)

Local models are slow. **Do not generate live in front of people.** Pre-run a requirement, then show the finished
result and run only the fast stage live.

1. Start Ollama and Docker Desktop.
2. Start the server in its own terminal:
   ```powershell
   cd <your project folder>
   .venv\Scripts\activate
   $env:AISEW_MODEL="qwen2.5-coder:3b"     # faster than 7b for a live BA stage
   uvicorn aisew.api.main:app --reload --reload-dir src
   ```
3. Open <http://localhost:8000/docs> in a browser tab.
4. Keep these ready in other tabs/windows:
   - `GET /requirements/1` (the finished booking-refund run) and `GET /requirements/2` (password reset)
   - the generated code folder in VS Code (`workspaces/project-1/requirement-1/backend`)
   - the repository page on GitHub with the architecture diagram
5. Check `GET /sandbox` shows `docker_available: true` and `image_ready: true`.

## The 5-minute flow

| Time | Show | Say |
|---|---|---|
| 0:00 | The README diagram on GitHub | "Five agents with a human approval gate between stages. DBA, Manager and QA work in parallel." |
| 0:45 | Docs page: `POST /requirements` with *"Customers should be able to cancel a booking and get a refund"* (live; answers in 1 second) | "The request returns immediately. The agents work in the background, so nothing hangs." |
| 1:15 | `GET /requirements/3` (or the new id) while it runs: `status: Running`, `running: [{agent: BA, seconds: ...}]` | "This is the progress view. It shows what is running and for how long." |
| 2:00 | The BA result: `open_questions` | "It did not invent a refund policy. It asked what it needed to know: the time limit, exceptions, the refund method." |
| 2:45 | Switch to the **finished** run: `GET /requirements/1`. Show `business_rules` and `acceptance_criteria` | "After my answers the rule became: full refund only when cancelled at least 24 hours before the booking starts." |
| 3:15 | Show the three parallel artifacts: DBA tables, Manager tasks, QA test cases (each QA test names the acceptance criterion it covers) | "These ran at the same time. Every number and rule traces back to the requirement or to my answers." |
| 3:45 | `GET /artifacts/{id}/code`, then `test_report` (`passed`, `rounds`, `auto_fixes`) | "The tests ran in Docker with no network. When they failed, it fixed what it could by rule first, then asked the model." |
| 4:30 | `audit_log` at the bottom of the response | "Every approval, answer and failure is recorded. Nothing reached disk until I approved." |
| 4:45 | Run `python -m pytest` in the generated backend folder | "The generated code passes its tests." |

## What to say about the weak spot (say it first, it earns trust)

> "The planning stages work well even on a small local model. The Developer stage is the hard part: small models
> write several files that do not agree with each other. So I built checks that catch it and show it, and for these
> two examples I reviewed and corrected the final code by hand. A stronger or hosted model for that stage is the
> next step."

Never present the generated code as fully automatic. The README says the same, and being honest here is a strength.

## Questions you will be asked

**Why local models?** Privacy and cost: no data leaves the machine, and no API bill. The design does not depend on it.
A different model is a one-line setting (`AISEW_MODEL`).

**How do you know the AI's output is right?** You do not trust it. Each output is validated against a schema, then
against rules a model cannot argue with: numbers that are not in the requirement are rejected, every acceptance
criterion must be covered by a test, generated Python must parse, and the database must be able to store what the
rules talk about. Failures go back to the agent once, and then are shown to you instead of hidden.

**Is it safe to run AI-written code?** It never runs on your machine. It runs in a container with no network, 768 MB
of memory, one CPU, no capabilities, a read-only filesystem and a time limit. The file writer rejects paths with `..`,
absolute paths and executable types.

**What happens when a model is slow or hangs?** Every model call has a timeout and an output limit, work runs in the
background, and a failed stage can be retried on its own without redoing the rest.

**What did you test?** 201 automated tests, none needing a model or Docker: each agent's quality rules, ordering and
parallel runs, failures and retries, the file writer's safety, the Docker command line, the background behaviour.
Testing also found a real concurrency bug (a lost database write when a status read ran on a different thread),
which is fixed and guarded by a test.

**What would you do next?** A hosted model for the Developer only, Code Review / Security / DevOps agents, and a
"reopen" action for approved results.

## One-page summary (paste into slides or an email)

**AISEW: multi-agent AI software engineering pipeline (Python, FastAPI, Ollama, Docker)**

- **Problem:** AI code generators skip the thinking: analysis, design, planning and tests.
- **Solution:** a pipeline of specialised agents (BA, DBA, Manager, QA, Developer) with human approval between stages.
- **Reliability:** schema validation, rule-based quality checks, retries, and a Docker test-and-fix loop with free
  rule-based fixes before the model is asked to repair anything.
- **Safety:** sandboxed execution, safe file writer, nothing written without approval, full audit trail.
- **Engineering:** background execution with timeouts, parallel stages, dependency-driven orchestration, 201 tests.
- **Honest limit:** small local models need a human check on the generated code. A stronger model is the next step.
