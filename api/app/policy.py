"""Moderation policy: one pure function, the only place thresholds live.

Rules (assignment, "The rules" 2 and 3):
  - any prohibited label >= 0.8 in any frame          -> blocked (every workspace)
  - any prohibited label >= 0.5 and < 0.8             -> held for a human
  - explicit >= 0.8 in an unverified workspace        -> blocked
  - explicit >= 0.8 in a verified workspace           -> allowed
Worst outcome across all frames wins: blocked > held > published.
"""

from dataclasses import dataclass, field
from typing import Literal

PROHIBITED = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person")
EXPLICIT = "explicit"
ALL_LABELS = PROHIBITED + (EXPLICIT,)

BLOCK_AT = 0.8
REVIEW_AT = 0.5

Decision = Literal["published", "held", "blocked"]
_SEVERITY: dict[str, int] = {"published": 0, "held": 1, "blocked": 2}


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reasons: list[str] = field(default_factory=list)  # stable codes, most severe first
    details: list[dict] = field(default_factory=list)  # [{label, score, frame_index, rule}]


def evaluate_policy(frames: list[dict[str, float]], workspace_verified: bool) -> PolicyDecision:
    """Evaluate every frame. `frames` must be canonical: every frame carries all six labels."""
    details: list[dict] = []
    for index, frame in enumerate(frames):
        for label in PROHIBITED:
            score = frame[label]
            if score >= BLOCK_AT:
                details.append({"label": label, "score": score, "frame_index": index, "rule": "block"})
            elif score >= REVIEW_AT:
                details.append({"label": label, "score": score, "frame_index": index, "rule": "review"})
        if frame[EXPLICIT] >= BLOCK_AT and not workspace_verified:
            details.append({"label": EXPLICIT, "score": frame[EXPLICIT], "frame_index": index, "rule": "block"})

    decision: Decision = "published"
    reasons: list[str] = []
    for item in details:
        outcome: Decision = "blocked" if item["rule"] == "block" else "held"
        if _SEVERITY[outcome] > _SEVERITY[decision]:
            decision = outcome
        code = "explicit_unverified" if item["label"] == EXPLICIT else f"{item['label']}_{item['rule']}"
        if code not in reasons:
            reasons.append(code)
    reasons.sort(key=lambda code: 0 if code.endswith(("_block", "_unverified")) else 1)
    return PolicyDecision(decision, reasons, details)
