"""Frozen Studio stage order and persistence state values."""

from __future__ import annotations

STUDIO_STAGES: tuple[str, ...] = (
    "PREPARE",
    "IMPORT_EXPORT",
    "VALIDATE_REGISTRY",
    "VALIDATE_VARIANTS",
    "VALIDATE_RENDERS",
    "EXTRACT_FEATURES",
    "AI_ANNOTATE",
    "VALIDATE",
    "HUMAN_REVIEW",
    "BUILD_RELEASE",
    "ACTIVATE_RELEASE",
)
R2_STAGES: tuple[str, ...] = STUDIO_STAGES[:6]

RUN_STATES = frozenset(
    {"pending", "running", "paused", "needs_review", "failed", "succeeded", "cancelled"}
)
ITEM_STATES = frozenset({"pending", "running", "succeeded", "needs_review", "failed", "skipped"})

R3_BOUNDARY_EVENT = "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"


class RunStateConflict(RuntimeError):
    """A persisted run/stage command is not legal in the current state."""

    code = "RUN_STATE_CONFLICT"


# The transition table is the single source used by service command checks.
# SQL CHECK constraints independently keep persisted values inside the same
# closed state sets.
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"paused", "needs_review", "failed", "succeeded", "cancelled"}),
    "paused": frozenset({"running"}),
    "needs_review": frozenset({"running"}),
    "failed": frozenset({"pending"}),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}
STAGE_TRANSITIONS = RUN_TRANSITIONS


def require_transition(current: str, target: str) -> None:
    if target not in RUN_TRANSITIONS.get(current, frozenset()):
        raise RunStateConflict(f"{current}->{target}")
