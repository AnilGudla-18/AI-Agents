"""BaseAgent: every agent only declares profile, rules, example and output schema."""
import json
import re
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from pydantic import ValidationError

from ..domain.enums import Readiness
from ..domain.outputs import AgentOutput
from ..llm.base import LLMClient, LLMResponse

T = TypeVar("T", bound=AgentOutput)

FEEDBACK_MARKER = "[reviewer feedback]"  # stored in the clarifications table

COMMON_RULES = [
    "Never invent facts or answers. If information is missing, add it to open_questions.",
    "Mark a question as blocking=true only if you cannot proceed without the answer.",
    "Do not re-ask questions that were already answered in the provided clarifications.",
    "confidence is an integer from 0 to 100. Be conservative: lower it when you had to ask questions.",
    "Do not contradict yourself.",
    "Respond with a single JSON object matching the schema. No prose, no markdown.",
]


class AgentOutputError(Exception):
    """Raised when the model cannot produce valid output after all retries."""


@dataclass
class AgentResult(Generic[T]):
    output: T
    attempts: int
    usage: list[LLMResponse] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)  # unresolved after retries


class BaseAgent(Generic[T]):
    name: ClassVar[str] = "agent"
    profile: ClassVar[str] = ""
    core_rules: ClassVar[list[str]] = []
    output_model: ClassVar[type[AgentOutput]] = AgentOutput
    examples: ClassVar[str] = ""  # one short example of a good answer (different topic)

    def __init__(self, llm: LLMClient, max_retries: int = 1):
        self.llm = llm
        self.max_retries = max_retries

    # ---- prompts -------------------------------------------------------
    def build_system_prompt(self) -> str:
        rules = "\n".join(f"- {r}" for r in [*COMMON_RULES, *self.core_rules])
        prompt = f"{self.profile}\n\nRules:\n{rules}"
        if self.examples:
            prompt += (
                "\n\nExample of a well-formed answer for a DIFFERENT topic "
                "(copy the style, never the content):\n" + self.examples
            )
        return prompt

    def build_user_prompt(self, context: dict) -> str:
        parts = [f"Requirement:\n{context['requirement']}"]
        confirmed = context.get("ba_clarifications") or []
        if confirmed:
            facts = "\n".join(f"- {c['question']} -> {c['answer']}" for c in confirmed)
            parts.append(
                "Facts confirmed by the business (ground truth: never contradict them, never re-ask them):\n"
                + facts
            )
        answered = context.get("clarifications") or []
        if answered:
            qa = "\n".join(f"Q: {c['question']}\nA: {c['answer']}" for c in answered)
            parts.append(f"Already answered clarifications (do not re-ask):\n{qa}")
        feedback = context.get("feedback") or []
        if feedback:
            points = "\n".join(f"- {f}" for f in feedback)
            parts.append(f"Reviewer feedback on your previous answer. Apply EVERY point:\n{points}")
            if context.get("previous_output"):
                prev = json.dumps(context["previous_output"], separators=(",", ":"))
                parts.append(f"Your previous answer (revise it, do not start from scratch):\n{prev}")
        return "\n\n".join(parts)

    # ---- quality hook (override in concrete agents) ----------------------
    def check_quality(self, output: T, context: dict) -> list[str]:
        """Return a list of problems with an otherwise schema-valid output."""
        return []

    # ---- run -----------------------------------------------------------
    async def run(self, context: dict) -> AgentResult[T]:
        system = self.build_system_prompt()
        user = self.build_user_prompt(context)
        schema = self.output_model.model_json_schema()
        usage: list[LLMResponse] = []
        feedback = ""
        last_error = ""
        fallback: tuple[T, list[str]] | None = None
        attempts = self.max_retries + 1

        for attempt in range(1, attempts + 1):
            resp = await self.llm.generate(system=system, user=user + feedback, schema=schema)
            usage.append(resp)
            try:
                output = self.output_model.model_validate(self._parse_json(resp.text))
            except (ValueError, ValidationError) as e:
                last_error = str(e)[:500]
                feedback = (
                    f"\n\nYour previous reply was invalid: {last_error}\n"
                    "Return ONLY valid JSON matching the schema."
                )
                continue

            issues = self.check_quality(output, context)  # type: ignore[arg-type]
            if not issues:
                return AgentResult(output=output, attempts=attempt, usage=usage)  # type: ignore[arg-type]
            fallback = (output, issues)  # type: ignore[assignment]
            feedback = (
                "\n\nYour previous reply had quality problems. Fix ALL of them and "
                "return the complete JSON again:\n" + "\n".join(f"- {i}" for i in issues)
            )

        if fallback:
            output, issues = fallback
            self._flag_issues(output, issues)
            return AgentResult(output=output, attempts=attempts, usage=usage, quality_issues=issues)

        raise AgentOutputError(f"{self.name}: invalid output after {attempts} attempts: {last_error}")

    @staticmethod
    def _flag_issues(output: AgentOutput, issues: list[str]) -> None:
        """Quality problems that survived the retries are surfaced, never hidden."""
        output.risks.extend(f"Quality check failed: {i}" for i in issues)
        output.confidence = min(output.confidence, 60)
        if output.readiness == Readiness.READY:
            output.readiness = Readiness.READY_WITH_CLARIFICATIONS

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        return json.loads(text)
