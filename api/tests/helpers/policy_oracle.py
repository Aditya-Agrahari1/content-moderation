"""Independent reference for the moderation rules, written as plainly as possible.

It deliberately shares no code with app/policy.py. Property tests compare the two, and the audit
uses this to decide what each published item *should* have been, from its original input frames.
Input frames may omit labels (an omitted label scores 0).
"""

PROHIBITED_LABELS = ["minor", "sexual_violence", "bestiality", "self_harm", "real_person"]


def expected_decision(frames: list[dict], workspace_verified: bool) -> str:
    worst = "published"
    for frame in frames:
        for label in PROHIBITED_LABELS:
            score = frame.get(label, 0.0)
            if score >= 0.8:
                return "blocked"
            if score >= 0.5:
                worst = "held"
        if not workspace_verified and frame.get("explicit", 0.0) >= 0.8:
            return "blocked"
    return worst
