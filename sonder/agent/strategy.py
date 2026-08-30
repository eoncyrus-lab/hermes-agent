"""Objective selection, then tactics. In that order (doc s28).

Deciding "what should this message accomplish" before "what should it say"
is the difference between a strategy and a clever line. It also makes the
prediction meaningful: you cannot predict the outcome of a tactic without
first naming what would count as success.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.schemas.preference import PreferenceModel
from ..domain.schemas.primitives import BeliefStatus
from ..domain.schemas.strategy import Objective
from .context import AgentContext


@dataclass(slots=True)
class Plan:
    objective: Objective
    tactic_keys: list[str]
    avoided_keys: list[str] = field(default_factory=list)
    rationale: str = ""
    predicted_positive: float = 0.5
    prediction_basis: str = ""
    next_milestone: str = ""
    exploration: bool = False


# Ordered: the first matching rule wins. Health outranks everything, because
# a system that optimises engagement while the other person is uncomfortable
# is the exact thing this product is not.
def choose_objective(ctx: AgentContext) -> tuple[Objective, str]:
    st = ctx.state_row

    if st is not None:
        health = st.health or {}

        def low(axis: str, bar: float) -> bool:
            a = health.get(axis)
            return bool(a and a.get("value", 1) < bar and a.get("confidence", 0) >= 0.35)

        def high(axis: str, bar: float) -> bool:
            a = health.get(axis)
            return bool(a and a.get("value", 0) > bar and a.get("confidence", 0) >= 0.35)

        if low("emotional_safety", 0.55) or high("pressure_exerted_by_self", 0.35):
            return (
                Objective.DE_ESCALATE,
                "health signals are the binding constraint; engagement is not "
                "the goal right now",
            )

    if st is None:
        return (
            Objective.ESTABLISH_RAPPORT,
            "no state computed yet; the goal is to learn, not to advance",
        )

    def val(group: str, axis: str) -> float | None:
        a = (getattr(st, group, None) or {}).get(axis)
        if a and a.get("confidence", 0) >= 0.6:
            return a.get("value")
        return None

    momentum = val("dynamics", "momentum")
    romantic = val("attraction", "romantic")
    conversational = val("intimacy", "conversational")
    emotional = val("intimacy", "emotional")

    if momentum is not None and momentum < 0.3:
        return (
            Objective.CLARIFY_INTENT,
            "momentum has fallen; guessing costs more than asking",
        )
    if romantic is not None and romantic > 0.6 and (conversational or 0) > 0.5:
        return Objective.MOVE_OFFLINE, "mutual signals are established online"
    if emotional is not None and emotional < 0.35:
        return Objective.DEEPEN_DISCLOSURE, "the exchange is still surface-level"
    if conversational is not None and conversational > 0.5:
        return (
            Objective.BUILD_TENSION,
            "comfort is established; ambiguity is what is missing",
        )
    return Objective.INCREASE_ENGAGEMENT, "the base of the interaction is still thin"


# Tactics that are never appropriate for a given objective, whatever the
# preference model says worked before.
FORBIDDEN: dict[Objective, tuple[str, ...]] = {
    Objective.DE_ESCALATE: (
        "push_pull",
        "teasing",
        "sexual_subtext",
        "future_projection",
        "dominant",
    ),
    Objective.REPAIR: ("teasing", "push_pull", "playful_challenge", "dominant"),
    Objective.CLARIFY_INTENT: ("subtext", "ambiguity", "push_pull"),
    Objective.ESTABLISH_RAPPORT: ("sexual_subtext", "dominant", "future_projection"),
}

DEFAULT_TACTICS: dict[Objective, tuple[str, ...]] = {
    Objective.ESTABLISH_RAPPORT: ("curiosity", "shared_context", "light_humor"),
    Objective.INCREASE_ENGAGEMENT: ("curiosity", "callback", "light_humor"),
    Objective.DEEPEN_DISCLOSURE: ("vulnerability", "specific_question", "validation"),
    Objective.BUILD_TENSION: ("teasing", "subtext", "role_contrast"),
    Objective.MOVE_OFFLINE: ("concrete_invitation", "shared_activity"),
    Objective.CLARIFY_INTENT: ("direct_statement", "low_pressure_question"),
    Objective.DE_ESCALATE: ("space", "validation", "plain_warmth"),
    Objective.REPAIR: ("acknowledgement", "responsibility", "validation"),
    Objective.DISENGAGE: ("plain_closure",),
    Objective.ADVANCE_PHYSICAL: (),
    Objective.DEFINE_RELATIONSHIP: ("direct_statement", "vulnerability"),
}


def plan(ctx: AgentContext) -> Plan:
    objective, rationale = choose_objective(ctx)
    forbidden = set(FORBIDDEN.get(objective, ()))
    candidates = [t for t in DEFAULT_TACTICS.get(objective, ()) if t not in forbidden]

    pm: PreferenceModel | None = ctx.preference
    known_good: list[str] = []
    known_bad: list[str] = []
    if pm:
        known_good = [k for k in pm.recommended() if k not in forbidden]
        known_bad = pm.avoid()

    # Preference-backed tactics lead; defaults fill the rest so the agent does
    # not narrow to the two things that once worked and stop learning.
    tactics = list(dict.fromkeys([*known_good[:2], *candidates]))[:3]
    avoided = sorted(set(known_bad) | forbidden)

    # Prediction. Grounded in this person's own record where it exists,
    # explicitly a base rate where it does not.
    exploration = False
    if pm and tactics and (entry := pm.get(tactics[0])):
        est = entry.estimate
        if entry.status is BeliefStatus.UNKNOWN:
            predicted, basis = (
                0.5,
                (
                    f"'{tactics[0]}' has n={est.n} for her -- not enough to predict; "
                    "treating this as an exploratory move"
                ),
            )
            exploration = True
        else:
            predicted = round(est.mean, 3)
            basis = f"'{tactics[0]}' {est.describe()} for her"
    else:
        predicted, basis = (
            0.5,
            ("no preference history for this tactic with this person; base rate only"),
        )
        exploration = True

    return Plan(
        objective=objective,
        tactic_keys=tactics,
        avoided_keys=avoided,
        rationale=rationale,
        predicted_positive=predicted,
        prediction_basis=basis,
        next_milestone=MILESTONES.get(objective, ""),
        exploration=exploration,
    )


MILESTONES: dict[Objective, str] = {
    Objective.ESTABLISH_RAPPORT: "she asks you something unprompted",
    Objective.INCREASE_ENGAGEMENT: "she continues a topic without being asked",
    Objective.DEEPEN_DISCLOSURE: "she volunteers something she has not said before",
    Objective.BUILD_TENSION: "she returns the register rather than defusing it",
    Objective.MOVE_OFFLINE: "a specific time is agreed",
    Objective.CLARIFY_INTENT: "you have an answer either way",
    Objective.DE_ESCALATE: "she sets the pace for the next exchange",
    Objective.REPAIR: "she engages with the substance rather than the apology",
}
