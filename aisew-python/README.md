# AISEW (Python)

Multi-agent AI software engineering pipeline.

| Phase | What it does |
|---|---|
| 1 | Foundation: BaseAgent, Ollama client, BA agent |
| 2 | DBA agent, SQLite database, FastAPI, approval + clarification loop, quality checks |
| 3 | Manager + QA agents, automatic orchestration (parallel), safer `/reply`, feedback endpoint |
| 4 | Developer agent + safe workspace writer: approved plan -> real code files on disk |

## Setup
```bash
python -m venv .venv
.venv\Scripts\activate          # Mac/Linux: source .venv/bin/activate
pip install -e ".[dev]"
ollama pull llama3.2
```

## Tests (no Ollama needed)
```bash
pytest
```

## Run the API
```bash
# Command Prompt:  set AISEW_MODEL=llama3.2:latest
# PowerShell:      $env:AISEW_MODEL="llama3.2:latest"
uvicorn aisew.api.main:app --reload --reload-dir src
```
`--reload-dir src` matters: without it the server restarts itself whenever generated code lands in
`workspaces/` (new .py files), which would kill a running agent.

Open http://localhost:8000/docs

Optional settings (environment variables):
- `AISEW_MAX_TOKENS=6000` longest answer one model call may write. A runaway answer is cut off and retried.
- `AISEW_TIMEOUT=900` seconds one model call may take before it fails (then retry with `/run`).
- `AISEW_SANDBOX=docker|off`, `AISEW_FIX_ROUNDS=2`, `AISEW_TEST_TIMEOUT=120` control the test loop.
- `AISEW_MAX_PARALLEL=1` runs one agent at a time (use it if your PC struggles).
- `AISEW_NUM_CTX=8192` model context size. The Developer prompt is long: use 16384 if your RAM allows.

  Lower it (4096) if you run out of memory.

## Background runs (default)
Agents can take many minutes on a local model, so the server does NOT keep your request open.
`POST /requirements`, `/approve`, `/reply`, `/feedback` and `/run` answer immediately, and the agents
keep working in the background.
- `GET /requirements/{id}` is your progress page: `status` is `Running` while agents work, and
  `running` lists each one with the seconds it has been running.
- When `running` is empty, read `awaiting` (what to do next) and `needs_retry` (failed stages).
- A stage that is already running cannot be started again (409 / ignored by `/run`).
- A failure is written to `audit_log` (for example `DeveloperFailed` with the reason).
- `AISEW_BACKGROUND=0` goes back to the old behaviour (the request waits until the agents finish).

## Flow
```
BA  â”€â”€approveâ”€â”€â–º  DBA      â”€â”
                  Manager   â”œâ”€ run automatically, in parallel
                  QA       â”€â”˜
                             â””â”€ when all three are approved â”€â”€â–º  Developer â”€â”€approveâ”€â”€â–º code on disk
```
1. `POST /requirements`            -> BA runs
2. `POST /artifacts/{id}/reply`    -> answer the BA's questions (real answers only)
3. `POST /artifacts/{id}/approve`  -> approve BA: DBA, Manager and QA start by themselves
4. Approve DBA, Manager and QA     -> requirement status becomes `Complete`
5. `GET /requirements/{id}`        -> look at `awaiting` (what to do next) and `needs_retry`
6. `POST /requirements/{id}/run`   -> retry a stage that failed
7. `POST /artifacts/{id}/feedback` -> correct ANY pending artifact, even one with no open questions;
   the agent revises its previous answer. Body: `{"feedback": "what must change"}`

`awaiting[].action` tells you what to do: `reply` (answer its questions), `review` (it has unresolved
quality problems - read `quality_issues`, then give feedback or approve anyway) or `approve`.
Answers you give the BA are passed to DBA, Manager and QA as confirmed facts.

## The Developer stage
When DBA, Manager and QA are approved, the Developer agent writes the backend
(Python / FastAPI / SQLAlchemy + pytest tests) in two steps, because small models cannot write
code inside a JSON answer:
1. **Plan**: a short JSON answer listing the files and a one-sentence purpose for each.
2. **Write**: one plain-text model call per file. Each file is checked on its own (empty? TODO or
   placeholder? Python syntax? does `app/models.py` define every table?) and only that file is
   retried. Later files see the files already written, so imports match.

Feedback (`POST /artifacts/{id}/feedback`) works on the Developer too: the plan is revised and each
file is rewritten from its previous version, applying your feedback.
- `GET /artifacts/{id}/code` shows the generated files as readable text. Read them before approving.
- **Nothing is written to disk until you approve the Developer artifact.**
- Approved files go to `workspaces/project-<p>/requirement-<r>/backend/`
  (change the base folder with `AISEW_WORKSPACE_DIR`). The response shows the exact folder in `workspace`.
- The writer only accepts relative paths of known text file types (.py, .md, .txt, .json, ...).
  Anything with `..`, an absolute path, a drive letter, an executable type or an oversized file is
  skipped and logged as `DeveloperFileSkipped` in the audit log. The rest is still written.
- AISEW never runs the generated code on your own machine. Tests run only inside the Docker sandbox below.

## Test-and-fix loop (needs Docker Desktop)
After the Developer writes the code, AISEW runs the generated `pytest` tests in a locked-down Docker
container. If they fail, it reads the traceback, rewrites only the files it points at (at most 3 per
round) and runs the tests again, up to `AISEW_FIX_ROUNDS` times (default 2).
- The container has **no network**, 768 MB memory, 1 CPU, no capabilities, a read-only root filesystem,
  and a time limit (`AISEW_TEST_TIMEOUT`, default 120 s). The code is copied to a temporary folder,
  never to your workspace, and the folder is deleted afterwards.
- Allowed packages (installed in the image once): sqlalchemy, fastapi, pydantic, pytest,
  pytest-asyncio, httpx, anyio. Anything else in `requirements.txt` is reported.
- Setup once: install Docker Desktop and start it, then `GET /sandbox` (is `docker_available` true?),
  then `POST /sandbox/build` (a few minutes, runs in the background; `image_ready` becomes true).
- **Free fixes first.** Before the model is asked to rewrite anything, AISEW fixes by rule the mistakes
  small models make all the time: a missing import (`NameError: name 'Float' is not defined` -> adds
  `from sqlalchemy import Float`, also relationship, func, FastAPI, BaseModel, datetime, Optional, ...)
  and misspelled Column arguments (`PrimaryKey=` -> `primary_key=`), and files that disagree about a name
  (`ImportError: cannot import name 'send_email' from 'app.email_service'` -> adds `send_email = send_reset_email`
  when exactly one function there is clearly the same thing, or re-exports it from the file that defines it;
  it never guesses between two candidates). They cost no model call and no fix
  round, and every one is listed in `test_report.auto_fixes` and in the audit log (`DeveloperAutoFix`).
- **`POST /artifacts/{id}/retest`** runs a pending Developer artifact's tests again (rule fixes first, the
  model only if they are not enough). Use it after installing Docker, or after upgrading AISEW, to fix an
  existing result in seconds without regenerating the code.
- The result is in the Developer artifact as `test_report` (`ran`, `passed`, `rounds`, `summary`,
  `output`) and in the audit log (`DeveloperTestsPassed`, `DeveloperTestsFailed`, `DeveloperTestsNotRun`).
  Tests that still fail after the last round make the artifact `review` with the reason in `quality_issues`.
- No Docker? Nothing breaks: the tests are reported as `not run`. `AISEW_SANDBOX=off` switches it off.

Data is stored in `aisew.db` (SQLite) in the folder you start the server from.
Delete that file to start fresh.

## Layout
- `domain/` output contracts - `llm/` Ollama + fake client - `agents/` BA, DBA, Manager, QA
- `persistence/` SQLAlchemy models - `orchestration/` pipeline (PIPELINE table = stage dependencies)
- `api/` FastAPI

