"""Transport types shared by the research Workflow and its Activities.

Plain dataclasses with no third-party imports: Temporal's default converter carries
them across the Activity boundary, and `web.py` reads them as plain JSON from
Queries, so the web process never needs either provider SDK or an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from llm import Source, Usage


@dataclass
class ResearchRequest:
    """Durable Workflow input. Provider selection is per run, never ambient."""

    question: str
    provider: str = "gemini"


@dataclass
class SubQuestion:
    index: int
    text: str


@dataclass
class ResearchPlan:
    """The planner's output, WITH its token spend.

    Carrying `usage` is why this type exists rather than a bare list: returning
    `list[SubQuestion]` silently dropped one real model call per question, and a
    completed plan displayed `tokens: 0`. If an Activity calls Gemini, its return
    type must carry `Usage`.
    """

    sub_questions: list[SubQuestion] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class Finding:
    """The result of researching one sub-question."""

    index: int
    question: str
    summary: str
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    rounds: int = 1

    # Temporal's Activity attempt number, and THE interruption signal that actually
    # fires in production. `attempt > 1` means interrupted and re-run.
    attempt: int = 1

    # Gemini leaves this false because GenerateContent is atomic. Claude sets it
    # when a retry receives partial work from a pause_turn heartbeat checkpoint.
    resumed: bool = False


@dataclass
class Answer:
    text: str
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class Checkpoint:
    """Small liveness/resume payload carried by Activity heartbeats."""

    phase: str = "model_call"
    provider: str = ""
    rounds_done: int = 0
    partial_summary: str = ""
    tokens_so_far: int = 0
