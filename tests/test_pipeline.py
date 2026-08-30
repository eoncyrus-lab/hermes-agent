"""Behavioural tests for the engines. These encode the calibration decisions,
so a future change that quietly makes the agent more confident fails here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sonder.agent.context import AgentContext
from sonder.agent.critic import cross_dyad_leak, rank
from sonder.agent.humanizer import ai_pattern_score
from sonder.agent.orchestrator import Intent, classify, health_warning, route_request
from sonder.agent.strategy import plan
from sonder.analyzer.features import Baseline, extract, visible_length
from sonder.domain.schemas.interaction import Signal, SignalType, Speaker
from sonder.domain.schemas.strategy import CriticScores, ObservedOutcome, OutcomeLabel
from sonder.learning.outcomes import calibration, label, recompute_priors
from sonder.state.relationship import compute
from sonder.state.trend import compute_trend
from sonder.storage.scope import Scope

NOW = datetime.now(UTC)
RID, UID, PID, IID = uuid4(), uuid4(), uuid4(), uuid4()


def sig(t, mag=0.7, conf=0.8, by=Speaker.TARGET, days=0):
    return Signal(
        relationship_id=RID,
        interaction_id=IID,
        type=t,
        occurred_at=NOW - timedelta(days=days),
        magnitude=mag,
        confidence=conf,
        by=by,
        evidence_message_ids=(uuid4(),),
        extractor_version="test",
    )


def state(signals, version=1):
    return compute(
        signals,
        relationship_id=RID,
        user_id=UID,
        target_person_id=PID,
        version=version,
        supersedes=None if version == 1 else uuid4(),
        interaction_ids=[IID],
        now=NOW,
    )


# ------------------------------------------------------- state calibration


def test_politeness_does_not_become_attraction():
    """The headline failure mode. One warm exchange must not read as interest."""
    st = state(
        [sig(SignalType.ENGAGEMENT_UP, 0.5, 0.6), sig(SignalType.WARMTH, 0.4, 0.6)]
    )
    assert "attraction.romantic" not in st.actionable_axes()
    assert st.stage.value != "s4_flirting"


def test_absence_of_evidence_is_not_cooling():
    st = state([sig(SignalType.ENGAGEMENT_UP, 0.5, 0.6)])
    assert st.stage.value != "sx_cooling"


def test_sustained_reciprocity_does_register():
    signals = (
        [sig(SignalType.FLIRT_RECIPROCATED, 0.8, 0.8, days=d) for d in (1, 4, 9, 14)]
        + [sig(SignalType.SELF_DISCLOSURE, 0.7, 0.8, days=d) for d in (2, 6, 11)]
        + [sig(SignalType.PLAN_ACCEPTED, 0.9, 0.9, days=3)]
    )
    st = state(signals)
    romantic = st.attraction.romantic
    assert romantic.is_actionable
    assert romantic.value > 0.55
    assert romantic.prior == 0.25, "the prior must survive into the record"

    # Only signals that actually bear on this axis contribute: the four
    # reciprocated-flirt signals plus the accepted plan. Self-disclosure is
    # deliberately absent -- it is evidence about emotional intimacy and
    # trust, and letting it leak into an attraction score is how every axis
    # ends up measuring the same thing.
    assert len(romantic.evidence) == 5
    assert st.intimacy.emotional is not None
    assert st.bond.trust is not None


def test_prior_is_recorded_so_the_reading_is_auditable():
    st = state([sig(SignalType.FLIRT_RECIPROCATED, 0.8, 0.8, days=d) for d in range(6)])
    a = st.attraction.romantic
    assert a.prior is not None and a.method and a.model_version


def test_chemistry_and_health_move_independently():
    """doc s30: 'chemistry up, health down' must be representable."""
    signals = [
        sig(SignalType.FLIRT_RECIPROCATED, 0.8, 0.8, days=d) for d in (1, 3, 5, 8)
    ] + [sig(SignalType.SELF_DISCLOSURE, 0.7, 0.8, days=d) for d in (2, 6, 9)]
    healthy = state(signals)
    unhealthy = state(signals + [sig(SignalType.DISCOMFORT, 0.8, 0.85, days=1)])

    assert not healthy.health_flag
    assert unhealthy.health_flag
    assert unhealthy.attraction.romantic.value == pytest.approx(
        healthy.attraction.romantic.value, abs=0.02
    ), "a discomfort signal must not be laundered into lower attraction"


def test_one_discomfort_signal_is_enough_to_warn():
    """Asymmetric errors: warning early is the cheaper mistake."""
    st = state([sig(SignalType.DISCOMFORT, 0.8, 0.85)])
    assert st.health_flag


# ------------------------------------------------------------------ trends


def test_trend_needs_points_time_and_movement():
    def st_row(days_ago, val, conf=0.7):
        return SimpleNamespace(
            as_of=NOW - timedelta(days=days_ago),
            dynamics={"initiative_balance": {"value": val, "confidence": conf}},
            attraction={},
            intimacy={},
            bond={},
            health={},
        )

    rising = [st_row(30, 0.10), st_row(20, 0.17), st_row(10, 0.35), st_row(2, 0.42)]
    assert compute_trend(rising, "dynamics.initiative_balance").is_reportable

    two_points = [st_row(20, 0.1), st_row(2, 0.5)]
    assert not compute_trend(two_points, "dynamics.initiative_balance").is_reportable

    low_conf = [st_row(30, 0.1, 0.3), st_row(15, 0.9, 0.3), st_row(2, 0.2, 0.3)]
    assert compute_trend(low_conf, "dynamics.initiative_balance").n_points == 0


# ---------------------------------------------------------------- strategy


def test_health_outranks_engagement_in_objective_choice():
    sc = Scope(UID, RID)
    hot = SimpleNamespace(
        as_of=NOW,
        attraction={"romantic": {"value": 0.75, "confidence": 0.8}},
        intimacy={"conversational": {"value": 0.65, "confidence": 0.75}},
        bond={},
        dynamics={},
        health={},
        stage="s5",
    )
    assert plan(AgentContext(scope=sc, state_row=hot)).objective.value == "move_offline"

    hot.health = {"emotional_safety": {"value": 0.35, "confidence": 0.45}}
    p = plan(AgentContext(scope=sc, state_row=hot))
    assert p.objective.value == "de_escalate"
    assert "teasing" in p.avoided_keys and "push_pull" in p.avoided_keys


def test_cold_start_predicts_the_base_rate_and_says_so():
    p = plan(AgentContext(scope=Scope(UID, RID)))
    assert p.predicted_positive == 0.5
    assert p.exploration
    assert "base rate" in p.prediction_basis or "not enough" in p.prediction_basis


# --------------------------------------------------------------- humanizer


@pytest.mark.parametrize(
    "text,should_flag",
    [
        ("看来你今天很累，那我是不是应该少打扰你一点。", True),
        ("突然不太想学这么快了。", False),
        ("It seems like you had a rough day, that said, rest well!", True),
        ("明天有空吗", False),
    ],
)
def test_ai_patterns_are_detected(text, should_flag):
    assert (ai_pattern_score(text) > 0.3) is should_flag


def test_the_documented_humanizer_example_improves():
    before = "你这么耐心教我，我现在都有一点不想那么快学会了。"
    after = "突然不太想学这么快了。"
    assert ai_pattern_score(after) <= ai_pattern_score(before)
    assert len(after) < len(before)


# ------------------------------------------------------------------ critic


def test_leak_detection_is_bilingual_and_survives_rewording():
    others = {"咖啡店那只猫", "the penguin thing at the aquarium"}
    assert cross_dyad_leak("还在想咖啡店那只猫", others)[0] > 0
    assert cross_dyad_leak("that penguin thing at the aquarium was funny", others)[0] > 0
    assert cross_dyad_leak("明天有空吗", others)[0] == 0


def _cs(**kw):
    base = dict(
        naturalness=0.8,
        voice_match=0.8,
        context_match=0.8,
        strategy_match=0.7,
        tension=0.5,
        cringe_risk=0.1,
        ai_pattern_risk=0.1,
        pressure=0.1,
        manipulation_risk=0.1,
        boundary_risk=0.1,
    )
    base.update(kw)
    return CriticScores(**base)


def test_ranking_drops_rather_than_discounts_hard_rejects():
    ranked = rank(
        [
            ("ok", _cs()),
            ("manipulative", _cs(manipulation_risk=0.8)),
            ("leaky", _cs(cross_dyad_leak_risk=0.6)),
            ("boundary", _cs(boundary_risk=0.7)),
        ]
    )
    assert [t for t, _, _ in ranked] == ["ok"]


def test_pressure_is_penalised_not_rewarded():
    ranked = rank([("calm", _cs()), ("pushy", _cs(pressure=0.9))])
    assert ranked[0][0] == "calm"


# ---------------------------------------------------------------- learning


def test_outcome_scoring_refuses_to_reward_discomfort():
    good_metrics_bad_outcome = ObservedOutcome(
        reply_latency_delta=-0.9,
        reply_length_delta=0.8,
        flirt_reciprocated=True,
        discomfort_or_boundary=True,
    )
    assert label(good_metrics_bad_outcome)[0] is OutcomeLabel.NEGATIVE


def test_nothing_observed_is_not_a_neutral_result():
    lbl, valid = label(ObservedOutcome())
    assert lbl is OutcomeLabel.INDETERMINATE and not valid


def test_priors_need_three_relationships():
    a, b, c = uuid4(), uuid4(), uuid4()
    assert recompute_priors({a: {"teasing": (4, 5)}, b: {"teasing": (2, 4)}}) == {}
    assert "teasing" in recompute_priors(
        {a: {"teasing": (4, 5)}, b: {"teasing": (2, 4)}, c: {"teasing": (3, 3)}}
    )


def test_calibration_detects_overconfidence():
    overconfident = [(0.9, OutcomeLabel.NEGATIVE)] * 5
    assert calibration(overconfident)["bias"] > 0.5


# ------------------------------------------------------------------ intent


@pytest.mark.parametrize(
    "text,expected",
    [
        ("她刚说这个我怎么回", Intent.REPLY),
        ("最近是不是降温了", Intent.RELATIONSHIP_REVIEW),
        ("我们吵架了", Intent.CONFLICT),
        ("我是不是上头了", Intent.SELF_REFLECTION),
    ],
)
def test_intent_routing(text, expected):
    assert classify(text) is expected


def test_only_reply_takes_the_hot_path():
    assert Intent.REPLY.is_hot
    assert not Intent.RELATIONSHIP_REVIEW.is_hot


def test_health_warning_is_unprompted():
    st = SimpleNamespace(
        health={
            "emotional_safety": {"value": 0.35, "confidence": 0.45},
            "pressure_exerted_by_self": {"value": 0.5, "confidence": 0.5},
        }
    )
    warning = health_warning(st)
    assert warning and "pressure" in warning
    assert health_warning(SimpleNamespace(health={})) is None


# ---------------------------------------------------------------- features


def test_cjk_and_latin_lengths_are_comparable():
    assert visible_length("你好") == 4
    assert visible_length("hello") == 5


def test_baseline_suppresses_signals_it_cannot_support():
    thin = Baseline([600, 540], [20, 25])
    assert not thin.established
    assert thin.latency_z(60) is None, "no baseline means no latency signal"

    solid = Baseline([600] * 8, [20] * 8)
    assert solid.latency_z(60) > 0.5


def test_question_detection_handles_mid_sentence_cjk_markers():
    assert extract("明天有空吗？一起吃个饭").is_question


# ------------------------------------------------------- context rendering


def test_uncertain_readings_are_rendered_as_uncertain():
    """The render is the last place a hedge can be lost before the prompt."""
    ctx = AgentContext(
        scope=Scope(UID, RID),
        state_row=SimpleNamespace(
            as_of=NOW,
            attraction={
                "romantic": {"value": 0.72, "confidence": 0.42},
                "intellectual": {"value": 0.66, "confidence": 0.81},
            },
            intimacy={},
            bond={},
            dynamics={},
            health={},
            stage="s4_flirting",
        ),
    )
    rendered = ctx.render_state()
    assert "UNCERTAIN" in rendered and "do not rely on this" in rendered
    assert "attraction.romantic" in rendered
    assert "attraction.intellectual: 0.66" in rendered
    assert "label only" in rendered, "stage must be marked as non-decisional"


def test_cold_start_render_instructs_the_model_to_say_unknown():
    rendered = AgentContext(scope=Scope(UID, RID)).render()
    assert "none computed yet" in rendered
    assert "Do not guess" in rendered
    assert "do not invent a personality" in rendered


# ------------------------------------------------------------ orm integrity


def test_every_column_default_actually_evaluates():
    """Callable defaults are only invoked at insert time.

    A NameError inside one is invisible to every test that inserts via raw
    SQL, and then fires on the first real write. Cheap to check here.
    """
    from sonder.storage.models import Base

    checked = 0
    for table in Base.metadata.tables.values():
        for col in table.columns:
            default = getattr(col.default, "arg", None)
            if callable(default):
                value = default(None)
                assert value is not None, f"{table.name}.{col.name}"
                checked += 1
    assert checked > 5, "expected callable defaults to exist"


def test_orm_and_sql_enum_columns_agree():
    """The mapping must use the Postgres enum types the migration creates.

    Declaring them as String passes every offline test and fails only on a
    real insert, because asyncpg will not cast varchar into an enum column.
    """
    import re
    from pathlib import Path

    from sqlalchemy.dialects.postgresql import ENUM

    from sonder.storage.models import Base

    sql = (Path(__file__).parent.parent / "migrations" / "001_core.sql").read_text()
    declared = set(re.findall(r"CREATE TYPE (\w+) AS ENUM", sql))
    assert declared, "migration should declare enum types"

    for table in Base.metadata.tables.values():
        for col in table.columns:
            # find columns the SQL declares with an enum type
            pattern = rf"^\s+{col.name}\s+({'|'.join(declared)})\b"
            block = re.search(rf"CREATE TABLE {table.name} \((.*?)\n\);", sql, re.DOTALL)
            if not block:
                continue
            if re.search(pattern, block.group(1), re.MULTILINE):
                assert isinstance(col.type, ENUM), (
                    f"{table.name}.{col.name} is an enum in SQL but "
                    f"{type(col.type).__name__} in the ORM"
                )


# ------------------------------------------------------------ track routing


def test_intimate_intent_selects_the_intimate_track():
    from sonder.llm import Track

    assert classify("我们性生活最近有点问题") is Intent.INTIMACY
    assert Intent.INTIMACY.track is Track.INTIMATE
    assert Intent.REPLY.track is Track.STANDARD
    assert Intent.CONFLICT.track is Track.STANDARD


def test_specific_hints_beat_generic_ones():
    """The ordering bug this list exists to prevent."""
    # contains both 性生活 and 最近
    assert classify("我们性生活最近有点问题") is Intent.INTIMACY
    # contains only 最近
    assert classify("最近是不是降温了") is Intent.RELATIONSHIP_REVIEW


def test_exact_intimate_prefix_overrides_track_and_is_stripped():
    from sonder.llm import Track

    request = route_request("/intimate 她昨天说想试试新东西")
    assert request.text == "她昨天说想试试新东西"
    assert request.track is Track.INTIMATE
    assert route_request("/intimately hello").track is Track.STANDARD
    assert route_request("先说 /intimate hello").track is Track.STANDARD


async def test_signal_batch_deduplicates_same_underlying_evidence(monkeypatch):
    import sonder.analyzer.signals as signal_mod

    message_id = uuid4()
    messages = [
        SimpleNamespace(
            id=message_id,
            sender=Speaker.TARGET,
            text="yes",
            sent_at=NOW,
            reply_latency_s=None,
        )
    ]
    raw = signal_mod.RawSignal(
        type=SignalType.WARMTH.value,
        by=Speaker.TARGET.value,
        message_indices=[1],
        magnitude=0.5,
        confidence=0.6,
        rationale="warm reply",
    )

    class StubRouter:
        async def structured(self, *args, **kwargs):
            return signal_mod.SignalBatch(signals=[raw, raw])

    monkeypatch.setattr(signal_mod, "router", lambda: StubRouter())
    signals = await signal_mod.extract_signals(messages, RID, IID)

    assert len(signals) == 1
    again = await signal_mod.extract_signals(messages, RID, IID)
    assert again[0].id == signals[0].id


async def test_screenshot_job_replay_recomputes_after_post_commit_crash(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import sonder.worker.tasks as task_mod

    @asynccontextmanager
    async def existing_session(scope):
        # The job authorizes first, then looks for the already-ingested
        # interaction: the relationship it owns, then a row that is there.
        async def get(model, ident):
            if model.__name__ == "RelationshipRow":
                return SimpleNamespace(user_id=UID, target_person_id=uuid4())
            return object()

        yield SimpleNamespace(get=get)

    parse = AsyncMock()
    extract = AsyncMock()
    recompute = AsyncMock(return_value={"state_version": 2})
    monkeypatch.setattr(task_mod, "worker_session", existing_session)
    monkeypatch.setattr(task_mod.ingest_mod, "parse_screenshots", parse)
    monkeypatch.setattr(task_mod, "extract_signals", extract)
    monkeypatch.setattr(task_mod, "recompute_state", recompute)

    first_id = task_mod._screenshot_ingest_id(RID, "wechat", [b"same-image"])
    result = await task_mod.ingest_screenshots(
        {}, str(UID), str(RID), "wechat", [b"same-image"]
    )

    assert result == {"interaction_id": str(first_id), "duplicate": True}
    parse.assert_not_awaited()
    extract.assert_not_awaited()
    recompute.assert_awaited_once_with({}, str(UID), str(RID))


def test_safety_critical_tasks_cannot_run_on_a_permissive_provider(monkeypatch):
    """The expensive silent failure: a compliant model scoring boundary_risk
    returns low numbers for everything and the Critic stops working."""
    import sonder.config as config
    from sonder.llm import Task, Track
    from sonder.llm.router import Router, UnsafeRouting

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("SONDER_LOCAL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SONDER_PROVIDER_EXTRACT", "local")
    config.settings.cache_clear()

    r = Router()
    with pytest.raises(UnsafeRouting, match="safety-critical"):
        r.resolve(Task.EXTRACT, Track.STANDARD)
    with pytest.raises(UnsafeRouting):
        r.resolve(Task.EXTRACT, Track.INTIMATE)
    config.settings.cache_clear()


def test_local_declaration_cannot_unlock_safety_work(monkeypatch):
    import sonder.config as config
    from sonder.llm import Task
    from sonder.llm.router import Router, UnsafeRouting

    monkeypatch.setenv("SONDER_LOCAL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SONDER_LOCAL_IS_PERMISSIVE", "false")
    monkeypatch.setenv("SONDER_PROVIDER_EXTRACT", "local")
    config.settings.cache_clear()

    with pytest.raises(UnsafeRouting):
        Router().resolve(Task.EXTRACT)
    config.settings.cache_clear()


@pytest.mark.parametrize("slot", ["anthropic", "openai"])
def test_custom_compatible_endpoints_fail_closed(monkeypatch, slot):
    import sonder.config as config
    from sonder.llm import Task
    from sonder.llm.router import Router, UnsafeRouting

    monkeypatch.setenv(f"SONDER_{slot.upper()}_API_KEY", "test")
    monkeypatch.setenv(
        f"SONDER_{slot.upper()}_BASE_URL", f"https://custom.example/{slot}"
    )
    monkeypatch.setenv("SONDER_PROVIDER_EXTRACT", slot)
    config.settings.cache_clear()

    with pytest.raises(UnsafeRouting):
        Router().resolve(Task.EXTRACT)
    config.settings.cache_clear()


@pytest.mark.parametrize(
    ("slot", "url"),
    [
        ("anthropic", "https://api.anthropic.com"),
        ("openai", "https://api.openai.com/v1"),
    ],
)
def test_explicit_official_endpoints_remain_safety_trusted(monkeypatch, slot, url):
    import sonder.config as config
    from sonder.llm import Task
    from sonder.llm.router import Router

    monkeypatch.setenv(f"SONDER_{slot.upper()}_API_KEY", "test")
    monkeypatch.setenv(f"SONDER_{slot.upper()}_BASE_URL", url)
    monkeypatch.setenv("SONDER_PROVIDER_EXTRACT", slot)
    config.settings.cache_clear()

    assert Router().resolve(Task.EXTRACT).provider_name == slot
    config.settings.cache_clear()


async def test_safety_work_rejects_call_level_model_override(monkeypatch):
    import sonder.config as config
    from sonder.llm import Task
    from sonder.llm.router import Router, UnsafeRouting

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", "test")
    config.settings.cache_clear()
    r = Router()

    with pytest.raises(UnsafeRouting, match="call-level model override"):
        await r.complete(Task.CRITIC, [], model="unchecked-model")
    config.settings.cache_clear()


def test_intimate_track_routes_only_generation(monkeypatch):
    import sonder.config as config
    from sonder.llm import Task, Track
    from sonder.llm.router import Router

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("SONDER_LOCAL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SONDER_PROVIDER_GENERATE_INTIMATE", "local")
    monkeypatch.setenv("SONDER_MODEL_GENERATE_INTIMATE", "hermes-4-70b")
    config.settings.cache_clear()

    r = Router()
    assert r.resolve(Task.GENERATE, Track.INTIMATE).provider_name == "local"
    assert r.resolve(Task.GENERATE, Track.STANDARD).provider_name == "anthropic"
    # extraction and criticism stay put in BOTH tracks
    assert r.resolve(Task.EXTRACT, Track.INTIMATE).provider_name == "anthropic"
    assert r.resolve(Task.EXTRACT, Track.STANDARD).provider_name == "anthropic"
    config.settings.cache_clear()


def test_intimate_reason_has_a_real_override(monkeypatch):
    import sonder.config as config
    from sonder.llm import Task, Track
    from sonder.llm.router import Router

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("SONDER_LOCAL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SONDER_PROVIDER_REASON_INTIMATE", "local")
    monkeypatch.setenv("SONDER_MODEL_REASON_INTIMATE", "hermes-4-70b")
    config.settings.cache_clear()

    route = Router().resolve(Task.REASON, Track.INTIMATE)
    assert (route.provider_name, route.model) == ("local", "hermes-4-70b")
    config.settings.cache_clear()


def test_policy_failover_will_not_route_around_the_guard(monkeypatch):
    """Failover must not become a back door into the permissive provider."""
    import sonder.config as config
    from sonder.llm import Task, Track
    from sonder.llm.router import Route, Router

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("SONDER_LOCAL_BASE_URL", "http://localhost:8000/v1")
    config.settings.cache_clear()

    r = Router()
    route = Route(Task.EXTRACT, Track.STANDARD, "anthropic", "m", False)
    assert r._fallback(route) is None, "failover offered a permissive provider"
    config.settings.cache_clear()


@pytest.mark.parametrize(
    ("primary", "secondary", "expected_model"),
    [
        ("anthropic", "openai", "openai-generate"),
        ("openai", "anthropic", "anthropic-generate"),
    ],
)
async def test_policy_failover_uses_provider_specific_model(
    primary, secondary, expected_model
):
    from sonder.config import Settings
    from sonder.llm import Completion, ContentPolicyRefusal, Task
    from sonder.llm.router import Router

    class Provider:
        def __init__(self, refusal=False):
            self.refusal = refusal
            self.calls = []

        async def complete(self, messages, **kw):
            self.calls.append(kw["model"])
            if self.refusal:
                raise ContentPolicyRefusal("policy")
            return Completion("ok", kw["model"])

    cfg = Settings(
        anthropic_api_key="test",
        openai_api_key="test",
        provider_generate=primary,
        model_generate="anthropic-generate",
        model_openai_generate="openai-generate",
    )
    r = Router(cfg)
    first, second = Provider(refusal=True), Provider()
    r._providers = {primary: first, secondary: second}
    r._order = [primary, secondary]
    r._permissive = {primary: False, secondary: False}

    result = await r.complete(Task.GENERATE, [])

    assert first.calls == [
        "anthropic-generate" if primary == "anthropic" else "openai-generate"
    ]
    assert second.calls == [expected_model]
    assert result.model == expected_model


async def test_intimate_local_refusal_falls_back_to_hosted_model():
    from sonder.config import Settings
    from sonder.llm import Completion, ContentPolicyRefusal, Task, Track
    from sonder.llm.router import Router

    class Provider:
        def __init__(self, refusal=False):
            self.refusal = refusal
            self.calls = []

        async def complete(self, messages, **kw):
            self.calls.append(kw["model"])
            if self.refusal:
                raise ContentPolicyRefusal("policy")
            return Completion("ok", kw["model"])

    cfg = Settings(
        anthropic_api_key="test",
        local_base_url="http://localhost:8000/v1",
        provider_generate_intimate="local",
        model_generate_intimate="local-intimate",
        model_generate="hosted-generate",
    )
    r = Router(cfg)
    local, hosted = Provider(refusal=True), Provider()
    r._providers = {"anthropic": hosted, "local": local}
    r._order = ["anthropic", "local"]
    r._permissive = {"anthropic": False, "local": True}

    await r.complete(Task.GENERATE, [], track=Track.INTIMATE)

    assert local.calls == ["local-intimate"]
    assert hosted.calls == ["hosted-generate"]


def test_intimacy_skill_states_the_no_inference_rule():
    """The skill is the prompt on this track, so the rule has to be in it."""
    from sonder.agent import skills

    skills.registry.cache_clear()
    body = skills.registry()["intimacy"].body
    # structural, not phrasing: these sections must exist on this track
    assert "## Never infer what someone likes" in body
    assert "## Consent is a practice, not a gate" in body
    assert "## What this skill will not do" in body
    assert "preferences are known when they have said so" in body
    selected = [s.name for s in skills.select("INTIMACY")]
    assert "intimacy" in selected and "reasoning" in selected
