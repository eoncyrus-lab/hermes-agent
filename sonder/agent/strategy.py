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
                "健康信号是当前的硬约束，现在不是推进的时候",
            )

    if st is None:
        return (
            Objective.ESTABLISH_RAPPORT,
            "还没有算出状态；现在的目标是了解，不是推进",
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
            "势头掉下来了；猜的代价比问一句大",
        )
    if romantic is not None and romantic > 0.6 and (conversational or 0) > 0.5:
        return Objective.MOVE_OFFLINE, "线上已经互相给过信号了，可以约出来"
    if emotional is not None and emotional < 0.35:
        return Objective.DEEPEN_DISCLOSURE, "交流还停在表面"
    if conversational is not None and conversational > 0.5:
        return (
            Objective.BUILD_TENSION,
            "舒适已经有了，缺的是那点暧昧",
        )
    return Objective.INCREASE_ENGAGEMENT, "跟她之间的底子还很薄"


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
                f"'{tactics[0]}' 在她身上 {est.describe()}——不足以预测，这一步当作试探",
            )
            exploration = True
        else:
            predicted = round(est.mean, 3)
            basis = f"'{tactics[0]}' 在她身上 {est.describe()}"
    else:
        predicted, basis = (
            0.5,
            ("跟她之间这个套路还没有历史，只能用基础概率"),
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
    Objective.ESTABLISH_RAPPORT: "她会主动问你点什么",
    Objective.INCREASE_ENGAGEMENT: "不用你追问，她会自己接着聊下去",
    Objective.DEEPEN_DISCLOSURE: "她会主动说一件以前没提过的事",
    Objective.BUILD_TENSION: "她会接住那点暧昧，而不是打岔岔开",
    Objective.MOVE_OFFLINE: "定下具体时间",
    Objective.CLARIFY_INTENT: "不管答案是什么，你都有个数了",
    Objective.DE_ESCALATE: "下一轮由她来定节奏",
    Objective.REPAIR: "她接的是事情本身，而不是那句道歉",
}
