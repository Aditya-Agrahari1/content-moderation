import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy import ALL_LABELS, PROHIBITED, evaluate_policy
from tests.helpers.policy_oracle import expected_decision


def frame(**scores: float) -> dict[str, float]:
    return {label: scores.get(label, 0.0) for label in ALL_LABELS}


@pytest.mark.parametrize("label", PROHIBITED)
@pytest.mark.parametrize(
    "score,expected", [(0.4999, "published"), (0.5, "held"), (0.7999, "held"), (0.8, "blocked"), (1.0, "blocked")]
)
@pytest.mark.parametrize("verified", [True, False])
def test_prohibited_boundaries(label, score, expected, verified):
    assert evaluate_policy([frame(**{label: score})], verified).decision == expected


@pytest.mark.parametrize(
    "score,verified,expected",
    [(0.8, True, "published"), (1.0, True, "published"), (0.8, False, "blocked"),
     (0.7999, False, "published"), (0.4999, False, "published")],
)
def test_explicit_depends_on_verification(score, verified, expected):
    assert evaluate_policy([frame(explicit=score)], verified).decision == expected


@pytest.mark.parametrize("position", [0, 30, 59])
def test_every_frame_of_a_60_second_video_counts(position):
    frames = [frame(explicit=0.01) for _ in range(60)]
    frames[position] = frame(minor=0.95)
    result = evaluate_policy(frames, workspace_verified=True)
    assert result.decision == "blocked"
    assert result.details[0]["frame_index"] == position


def test_worst_outcome_wins_across_frames_and_labels():
    result = evaluate_policy([frame(self_harm=0.6), frame(explicit=0.9)], workspace_verified=False)
    assert result.decision == "blocked"
    assert result.reasons == ["explicit_unverified", "self_harm_review"]
    assert evaluate_policy([frame(minor=0.5, explicit=0.8)], workspace_verified=True).decision == "held"


def test_reason_codes():
    assert evaluate_policy([frame(real_person=0.85)], True).reasons == ["real_person_block"]
    assert evaluate_policy([frame(bestiality=0.55)], True).reasons == ["bestiality_review"]
    assert evaluate_policy([frame()], False).reasons == []


scores = st.one_of(st.sampled_from([0.0, 0.4999, 0.5, 0.7999, 0.8, 1.0]), st.floats(0, 1))
frames_strategy = st.lists(st.fixed_dictionaries({label: scores for label in ALL_LABELS}), min_size=1, max_size=40)


@settings(max_examples=300, derandomize=True)
@given(frames=frames_strategy, verified=st.booleans())
def test_policy_matches_independent_oracle(frames, verified):
    assert evaluate_policy(frames, verified).decision == expected_decision(frames, verified)
