"""Transport types shared by the research Workflow and its Activities.

Plain dataclasses with no third-party imports: Temporal's default converter carries
them across the Activity boundary, and `web.py` reads them as plain JSON from
Queries, so the web process never needs the Claude SDK or an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from llm import Source, Usage


@dataclass
class SubQuestion:
    index: int
    text: str


@dataclass
class ResearchPlan:
    """The planner's output, WITH its token spend.

    Carrying `usage` is why this type exists rather than a bare list: returning
    `list[SubQuestion]` silently dropped one real Opus 5 call per question, and a
    completed plan displayed `tokens: 0`. If an Activity calls Claude, its return
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
    # fires in production. `resumed` below needs a heartbeat to have carried partial
    # work, which needs a pause_turn boundary — measured never to happen, so keying
    # the durability credit on it left the counter at 0 during the one demo moment it
    # exists for. `attempt > 1` means interrupted and re-run.
    attempt: int = 1

    # True when a retry picked up partial work from a heartbeat. Correct and tested,
    # but dormant until a call actually crosses a pause_turn boundary.
    resumed: bool = False


@dataclass
class Answer:
    text: str
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class Checkpoint:
    """What an Activity records via `activity.heartbeat` so a retry can resume.

    Deliberately small — heartbeat details ride every heartbeat.
    """

    rounds_done: int = 0
    partial_summary: str = ""
    tokens_so_far: int = 0
