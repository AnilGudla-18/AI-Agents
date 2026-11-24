"""Shared output contract every agent must follow."""
from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import Readiness

# No agent may claim certainty, and unanswered questions lower the ceiling.
MAX_CONFIDENCE = 95
MAX_CONFIDENCE_WITH_QUESTIONS = 80
MAX_CONFIDENCE_BLOCKED = 50


class OpenQuestion(BaseModel):
    question: str
    blocking: bool = False


class AgentOutput(BaseModel):
    readiness: Readiness
    confidence: int = Field(ge=0, le=100)
    summary: str = ""
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        # Small models sometimes return 150, 0.9, "85" ... clamp instead of failing.
        value = int(float(v))
        return max(0, min(100, value))

    @field_validator("open_questions", mode="before")
    @classmethod
    def _coerce_questions(cls, v):
        # Small models often return plain strings instead of objects.
        if not v:
            return []
        return [{"question": q} if isinstance(q, str) else q for q in v]

    @model_validator(mode="after")
    def _self_correct(self):
        """Readiness and confidence must be consistent with the open questions."""
        blocking = any(q.blocking for q in self.open_questions)
        if blocking:
            self.readiness = Readiness.NOT_READY
        elif self.open_questions and self.readiness == Readiness.READY:
            self.readiness = Readiness.READY_WITH_CLARIFICATIONS

        cap = MAX_CONFIDENCE
        if blocking:
            cap = MAX_CONFIDENCE_BLOCKED
        elif self.open_questions:
            cap = MAX_CONFIDENCE_WITH_QUESTIONS
        self.confidence = min(self.confidence, cap)
        return self


# ---------------- Business Analyst ----------------
class UserStory(BaseModel):
    as_a: str
    i_want: str
    so_that: str = ""


class BAOutput(AgentOutput):
    user_stories: list[UserStory] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


# ---------------- Database Architect ----------------
class DbColumn(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    references: str | None = None  # "table.column"

    @field_validator("references", mode="before")
    @classmethod
    def _clean_reference(cls, v):
        if v is None:
            return None
        v = str(v).strip().replace(":", ".")
        return None if v.lower() in ("", "null", "none", "n/a") else v

    @model_validator(mode="after")
    def _pk_not_nullable(self):
        if self.primary_key:
            self.nullable = False
        return self


class DbTable(BaseModel):
    name: str
    purpose: str = ""
    columns: list[DbColumn] = Field(default_factory=list)


class DBAOutput(AgentOutput):
    tables: list[DbTable] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_relationships(self):
        # Never trust the model's own list; derive it from the foreign keys.
        self.relationships = [
            f"{t.name}.{c.name} -> {c.references}"
            for t in self.tables
            for c in t.columns
            if c.references
        ]
        return self


# ---------------- Delivery Manager ----------------
class PlanTask(BaseModel):
    title: str
    role: str = ""
    effort: str = "M"  # S | M | L  (never hours or days)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("effort", mode="before")
    @classmethod
    def _effort_size(cls, v):
        first = str(v or "M").strip().upper()[:1]
        return first if first in ("S", "M", "L") else "M"

    @field_validator("depends_on", mode="before")
    @classmethod
    def _deps_list(cls, v):
        if not v:
            return []
        return [v] if isinstance(v, str) else v


class ManagerOutput(AgentOutput):
    tasks: list[PlanTask] = Field(default_factory=list)
    milestones: list[str] = Field(default_factory=list)


# ---------------- QA Engineer ----------------
class QaTestCase(BaseModel):
    title: str
    kind: str = "functional"  # functional | negative | edge
    covers: list[int] = Field(default_factory=list)  # acceptance criteria numbers (1-based)
    steps: list[str] = Field(default_factory=list)
    expected: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        v = str(v or "").strip().lower()
        if v.startswith("neg"):
            return "negative"
        if v.startswith("edge") or v.startswith("bound"):
            return "edge"
        return "functional"

    @field_validator("covers", mode="before")
    @classmethod
    def _covers(cls, v):
        import re

        if v is None:
            return []
        if isinstance(v, (str, int)):
            v = [v]
        found = []
        for item in v:
            m = re.search(r"\d+", str(item))
            if m:
                found.append(int(m.group()))
        return sorted(set(found))


class QAOutput(AgentOutput):
    test_cases: list[QaTestCase] = Field(default_factory=list)


# ---------------- Software Developer ----------------
def _tidy_path(v) -> str:
    p = str(v or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


class CodeFile(BaseModel):
    path: str
    content: str = ""
    purpose: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, v):
        return _tidy_path(v)


class PlannedFile(BaseModel):
    """Step 1 of the Developer: which file, and what goes in it. No code yet."""
    path: str
    purpose: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, v):
        return _tidy_path(v)


class DeveloperPlan(AgentOutput):
    files: list[PlannedFile] = Field(default_factory=list)
    run_instructions: str = ""


class SandboxReport(BaseModel):
    """What happened when AISEW ran the generated tests in the sandbox."""
    ran: bool = False
    passed: bool | None = None
    rounds: int = 0  # automatic fix rounds that were needed
    summary: str = ""
    output: str = ""
    auto_fixes: list[str] = Field(default_factory=list)  # fixed by rule, without asking the model


class DeveloperOutput(AgentOutput):
    files: list[CodeFile] = Field(default_factory=list)
    run_instructions: str = ""
    test_report: SandboxReport | None = None
