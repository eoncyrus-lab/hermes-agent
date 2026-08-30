"""Repositories. Every dyad-scoped method takes a Scope as its first argument.

The pattern is deliberate: you cannot call these without having proved which
relationship you are in. Combined with RLS this gives two independent
failures-to-safe rather than one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.schemas import (
    Interaction,
    PreferenceEvidence,
    PreferenceModel,
    RelationshipState,
    Signal,
    StrategyOutcome,
    StrategyRecord,
)
from ..domain.schemas.preference import PreferenceEntry, UserTacticPrior
from .models import (
    ConsentRecordRow,
    InteractionRow,
    MemoryChunkRow,
    MessageRow,
    PreferenceEvidenceRow,
    PreferenceModelRow,
    RelationshipRow,
    RelationshipStateRow,
    SignalRow,
    StrategyOutcomeRow,
    StrategyRecordRow,
    UserTacticPriorRow,
)
from .scope import Scope, UserScope


class ScopeViolation(RuntimeError):
    """Raised when data does not belong to the scope it was loaded under."""


def _guard(scope: Scope, *ids: UUID) -> None:
    for rid in ids:
        if rid != scope.relationship_id:
            raise ScopeViolation(
                f"object belongs to relationship {str(rid)[:8]}, "
                f"scope is {str(scope.relationship_id)[:8]}"
            )


class InteractionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, scope: Scope, interaction: Interaction) -> UUID:
        _guard(scope, interaction.relationship_id)
        row = InteractionRow(
            id=interaction.id,
            relationship_id=interaction.relationship_id,
            channel=interaction.channel.value,
            source=interaction.source.value,
            started_at=interaction.started_at,
            ended_at=interaction.ended_at,
            message_count_self=interaction.message_count_self,
            message_count_target=interaction.message_count_target,
            initiated_by=interaction.initiated_by.value,
            ended_by=interaction.ended_by.value,
            median_latency_self_s=interaction.median_latency_self_s,
            median_latency_target_s=interaction.median_latency_target_s,
            source_artifact_id=interaction.source_artifact_id,
            sensitivity=interaction.sensitivity.value,
            extractor_version=interaction.extractor_version,
        )
        self.s.add(row)
        for m in interaction.messages:
            self.s.add(
                MessageRow(
                    id=m.id,
                    interaction_id=interaction.id,
                    sender=m.sender.value,
                    sent_at=m.sent_at,
                    seq=m.seq,
                    body=m.text,
                    text_sha256=m.text_sha256,
                    reply_latency_s=m.reply_latency_s,
                    features=m.features.model_dump() if m.features else None,
                    sensitivity=m.sensitivity.value,
                    redacted=m.redacted,
                )
            )
        await self.s.flush()
        return interaction.id

    async def recent(
        self, scope: Scope, limit: int = 20, since: datetime | None = None
    ) -> list[InteractionRow]:
        q = select(InteractionRow).where(
            InteractionRow.relationship_id == scope.relationship_id
        )
        if since:
            q = q.where(InteractionRow.started_at >= since)
        q = q.order_by(desc(InteractionRow.started_at)).limit(limit)
        return list((await self.s.execute(q)).scalars())

    async def messages_for(self, scope: Scope, interaction_id: UUID) -> list[MessageRow]:
        owner = await self.s.get(InteractionRow, interaction_id)
        if owner is None:
            return []
        _guard(scope, owner.relationship_id)
        q = (
            select(MessageRow)
            .where(MessageRow.interaction_id == interaction_id)
            .order_by(MessageRow.seq)
        )
        return list((await self.s.execute(q)).scalars())

    async def last_messages(self, scope: Scope, n: int = 30) -> list[MessageRow]:
        q = (
            select(MessageRow)
            .join(InteractionRow, MessageRow.interaction_id == InteractionRow.id)
            .where(InteractionRow.relationship_id == scope.relationship_id)
            .order_by(desc(MessageRow.sent_at))
            .limit(n)
        )
        rows = list((await self.s.execute(q)).scalars())
        return list(reversed(rows))


class SignalRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add_many(self, scope: Scope, signals: list[Signal]) -> int:
        for sig in signals:
            _guard(scope, sig.relationship_id)
            interaction = await self.s.get(InteractionRow, sig.interaction_id)
            if interaction is None:
                raise ScopeViolation("signal references an unavailable interaction")
            _guard(scope, interaction.relationship_id)
            evidence = list(sig.evidence_message_ids)
            rows = list(
                (
                    await self.s.execute(
                        select(MessageRow).where(MessageRow.id.in_(evidence))
                    )
                ).scalars()
            )
            if len({row.id for row in rows}) != len(set(evidence)) or any(
                row.interaction_id != sig.interaction_id for row in rows
            ):
                raise ScopeViolation(
                    "signal evidence messages are unavailable or outside its interaction"
                )
            self.s.add(
                SignalRow(
                    id=sig.id,
                    relationship_id=sig.relationship_id,
                    interaction_id=sig.interaction_id,
                    type=sig.type.value,
                    occurred_at=sig.occurred_at,
                    magnitude=sig.magnitude,
                    confidence=sig.confidence,
                    by_speaker=sig.by.value,
                    evidence_message_ids=evidence,
                    baseline_note=sig.baseline_note,
                    extractor_version=sig.extractor_version,
                )
            )
        await self.s.flush()
        return len(signals)

    async def since(self, scope: Scope, days: int = 30) -> list[SignalRow]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        q = (
            select(SignalRow)
            .where(
                SignalRow.relationship_id == scope.relationship_id,
                SignalRow.occurred_at >= cutoff,
            )
            .order_by(SignalRow.occurred_at)
        )
        return list((await self.s.execute(q)).scalars())


async def _insert_or_return_existing(
    session: AsyncSession,
    model,
    scope_clause,
    input_fingerprint: str,
    values: dict,
) -> UUID:
    """Insert a fingerprinted row, or return the id of the one already there.

    The check-then-insert version of this loses a race: two deliveries of the
    same job both find nothing, both insert, and one dies on the unique index
    -- which is the failure the fingerprint exists to prevent. `ON CONFLICT DO
    NOTHING` lets the loser converge on the winner's row instead.

    Targeted at the fingerprint index specifically. An untargeted
    `ON CONFLICT DO NOTHING` swallows every unique violation on the table, and
    the re-select below then finds nothing and raises `NoResultFound` from
    somewhere unrelated to the real conflict -- so a genuine constraint breach
    would surface as a missing row. Both tables key the fingerprint on
    `relationship_id`, which is also the only scope their RLS policies let the
    writing session see.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    inserted = (
        await session.execute(
            pg_insert(model)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["relationship_id", "input_fingerprint"]
            )
            .returning(model.id)
        )
    ).scalar()
    if inserted is not None:
        await session.flush()
        return inserted
    return (
        await session.execute(
            select(model.id).where(
                scope_clause, model.input_fingerprint == input_fingerprint
            )
        )
    ).scalar_one()


class StateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def current(self, scope: Scope) -> RelationshipStateRow | None:
        q = (
            select(RelationshipStateRow)
            .where(RelationshipStateRow.relationship_id == scope.relationship_id)
            .order_by(desc(RelationshipStateRow.version))
            .limit(1)
        )
        return (await self.s.execute(q)).scalars().first()

    async def history(self, scope: Scope, limit: int = 50) -> list[RelationshipStateRow]:
        q = (
            select(RelationshipStateRow)
            .where(RelationshipStateRow.relationship_id == scope.relationship_id)
            .order_by(RelationshipStateRow.version)
            .limit(limit)
        )
        return list((await self.s.execute(q)).scalars())

    async def append(
        self, scope: Scope, state: RelationshipState, *, input_fingerprint: str
    ) -> UUID:
        """Write one snapshot, or return the one those inputs already produced.

        `input_fingerprint` is a digest of the SIGNALS AND INTERACTIONS the
        snapshot was computed from, plus the model version -- see
        `state.relationship.input_fingerprint`. Deliberately not a digest of
        the output: a scorer change must produce a new snapshot, and a queue
        redelivery must not. Recomputation is triggered by ARQ retries and
        duplicate deliveries, so without this the same inputs pile up one
        version per delivery and the trend -- which IS the table -- becomes a
        record of how often the queue hiccuped.

        One thing the digest deliberately drops: time. `state.relationship`
        decays signals against `now`, so the same inputs computed a month
        later produce different numbers and the SAME fingerprint, and this
        returns the old snapshot. That is correct for every caller today --
        recomputation only ever follows new signals or a redelivery. It would
        stop being correct the moment a scheduled recompute is added purely to
        let old signals decay; that would need the window in the digest.
        """
        _guard(scope, state.relationship_id)
        seen = (
            await self.s.execute(
                select(RelationshipStateRow.id).where(
                    RelationshipStateRow.relationship_id == scope.relationship_id,
                    RelationshipStateRow.input_fingerprint == input_fingerprint,
                )
            )
        ).scalar()
        if seen is not None:
            return seen
        if state.supersedes is not None:
            previous = await self.s.get(RelationshipStateRow, state.supersedes)
            if previous is None:
                raise ScopeViolation("state supersedes an unavailable snapshot")
            _guard(scope, previous.relationship_id)
        source_ids = list(state.computed_from_interaction_ids)
        if source_ids:
            sources = list(
                (
                    await self.s.execute(
                        select(InteractionRow).where(InteractionRow.id.in_(source_ids))
                    )
                ).scalars()
            )
            if len({row.id for row in sources}) != len(set(source_ids)) or any(
                row.relationship_id != scope.relationship_id for row in sources
            ):
                raise ScopeViolation("state inputs cross the relationship scope")
        dump = state.model_dump(mode="json")
        return await _insert_or_return_existing(
            self.s,
            RelationshipStateRow,
            RelationshipStateRow.relationship_id == scope.relationship_id,
            input_fingerprint,
            dict(
                id=state.id,
                relationship_id=state.relationship_id,
                version=state.version,
                supersedes=state.supersedes,
                as_of=state.as_of,
                window_start=state.window_start,
                window_end=state.window_end,
                attraction=dump["attraction"],
                intimacy=dump["intimacy"],
                bond=dump["bond"],
                dynamics=dump["dynamics"],
                health=dump["health"],
                compatibility=dump["compatibility"],
                stage=state.stage.value,
                computed_from=source_ids,
                model_version=state.model_version,
                input_fingerprint=input_fingerprint,
            ),
        )


class PreferenceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add_evidence(self, scope: Scope, evidence: list[PreferenceEvidence]) -> int:
        relationship = await self.s.get(RelationshipRow, scope.relationship_id)
        if relationship is None or relationship.user_id != scope.user_id:
            raise ScopeViolation("relationship does not belong to the user scope")
        for e in evidence:
            _guard(scope, e.relationship_id)
            if e.person_id != relationship.target_person_id:
                raise ScopeViolation("evidence person is not the relationship target")
            if e.source_interaction_id is not None:
                source = await self.s.get(InteractionRow, e.source_interaction_id)
                if source is None:
                    raise ScopeViolation("preference cites an unavailable interaction")
                _guard(scope, source.relationship_id)
            if e.source_signal_ids:
                source_signals = list(
                    (
                        await self.s.execute(
                            select(SignalRow).where(
                                SignalRow.id.in_(list(e.source_signal_ids))
                            )
                        )
                    ).scalars()
                )
                if len({row.id for row in source_signals}) != len(
                    set(e.source_signal_ids)
                ) or any(
                    row.relationship_id != scope.relationship_id for row in source_signals
                ):
                    raise ScopeViolation("preference signal sources cross scope")
            if e.source_strategy_outcome_id is not None:
                source_outcome = await self.s.get(
                    StrategyOutcomeRow, e.source_strategy_outcome_id
                )
                if source_outcome is None:
                    raise ScopeViolation("preference cites an unavailable outcome")
                _guard(scope, source_outcome.relationship_id)
            self.s.add(
                PreferenceEvidenceRow(
                    id=e.id,
                    person_id=e.person_id,
                    relationship_id=e.relationship_id,
                    domain=e.domain.value,
                    key=e.key,
                    direction=e.direction.value,
                    strength=e.strength,
                    provenance=e.provenance.value,
                    observed_at=e.observed_at,
                    source_interaction_id=e.source_interaction_id,
                    source_signal_ids=list(e.source_signal_ids),
                    source_strategy_outcome_id=e.source_strategy_outcome_id,
                    stage_at_time=e.stage_at_time,
                    channel=e.channel,
                    in_group_setting=e.in_group_setting,
                    known_confounders=list(e.known_confounders),
                    excluded_from_learning=e.excluded_from_learning,
                    sensitivity=e.sensitivity.value,
                )
            )
        await self.s.flush()
        return len(evidence)

    async def evidence(self, scope: Scope) -> list[PreferenceEvidenceRow]:
        q = select(PreferenceEvidenceRow).where(
            PreferenceEvidenceRow.relationship_id == scope.relationship_id
        )
        return list((await self.s.execute(q)).scalars())

    async def current_model(self, scope: Scope) -> PreferenceModelRow | None:
        q = (
            select(PreferenceModelRow)
            .where(PreferenceModelRow.relationship_id == scope.relationship_id)
            .order_by(desc(PreferenceModelRow.version))
            .limit(1)
        )
        return (await self.s.execute(q)).scalars().first()

    async def save_model(
        self, scope: Scope, model: PreferenceModel, *, input_fingerprint: str
    ) -> UUID:
        """Write one preference model, or return the one those inputs produced.

        Same reasoning as `StateRepo.append`: the digest covers the evidence
        rows and priors that went in, so a replay converges and a scorer change
        does not.
        """
        _guard(scope, model.relationship_id)
        seen = (
            await self.s.execute(
                select(PreferenceModelRow.id).where(
                    PreferenceModelRow.relationship_id == scope.relationship_id,
                    PreferenceModelRow.input_fingerprint == input_fingerprint,
                )
            )
        ).scalar()
        if seen is not None:
            return seen
        relationship = await self.s.get(RelationshipRow, scope.relationship_id)
        if relationship is None or relationship.user_id != scope.user_id:
            raise ScopeViolation("relationship does not belong to the user scope")
        if model.person_id != relationship.target_person_id:
            raise ScopeViolation("preference model person is not the relationship target")
        dump = {k: v.model_dump(mode="json") for k, v in model.entries.items()}
        return await _insert_or_return_existing(
            self.s,
            PreferenceModelRow,
            PreferenceModelRow.relationship_id == scope.relationship_id,
            input_fingerprint,
            dict(
                id=model.id,
                person_id=model.person_id,
                relationship_id=model.relationship_id,
                version=model.version,
                entries=dump,
                scorer_version=model.scorer_version,
                input_fingerprint=input_fingerprint,
            ),
        )

    @staticmethod
    def hydrate(row: PreferenceModelRow) -> PreferenceModel:
        return PreferenceModel(
            id=row.id,
            version=row.version,
            person_id=row.person_id,
            relationship_id=row.relationship_id,
            entries={
                k: PreferenceEntry.model_validate(v) for k, v in row.entries.items()
            },
            scorer_version=row.scorer_version,
        )


class PriorRepo:
    """User-scoped. The controlled cross-relationship channel."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def for_user(self, scope: UserScope) -> dict[str, UserTacticPrior]:
        q = select(UserTacticPriorRow).where(UserTacticPriorRow.user_id == scope.user_id)
        rows = list((await self.s.execute(q)).scalars())
        from ..domain.schemas.preference import PreferenceDomain

        return {
            r.tactic_key: UserTacticPrior(
                user_id=r.user_id,
                tactic_key=r.tactic_key,
                domain=PreferenceDomain(r.domain),
                prior_alpha=r.prior_alpha,
                prior_beta=r.prior_beta,
                n_relationships=r.n_relationships,
            )
            for r in rows
        }


class StrategyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def record(self, scope: Scope, rec: StrategyRecord) -> UUID:
        _guard(scope, rec.relationship_id)
        if rec.user_id != scope.user_id:
            raise ScopeViolation("strategy user does not match the user scope")
        state = await self.s.get(RelationshipStateRow, rec.relationship_state_id)
        if state is None:
            raise ScopeViolation("strategy references an unavailable state")
        _guard(scope, state.relationship_id)
        if rec.preference_model_id is not None:
            preference = await self.s.get(PreferenceModelRow, rec.preference_model_id)
            if preference is None:
                raise ScopeViolation(
                    "strategy references an unavailable preference model"
                )
            _guard(scope, preference.relationship_id)
        self.s.add(
            StrategyRecordRow(
                id=rec.id,
                relationship_id=rec.relationship_id,
                user_id=rec.user_id,
                objective=rec.objective.value,
                tactic_keys=list(rec.tactic_keys),
                avoided_keys=list(rec.avoided_keys),
                relationship_state_id=rec.relationship_state_id,
                preference_model_id=rec.preference_model_id,
                skills_invoked=list(rec.skills_invoked),
                rendered_text_sha256=rec.rendered_text_sha256,
                rendered_text=rec.rendered_text,
                n_candidates=rec.n_candidates,
                critic=rec.critic.model_dump() if rec.critic else None,
                predicted_positive=rec.predicted_positive,
                predicted_detail=rec.predicted_detail,
                prediction_basis=rec.prediction_basis,
                observation_window_s=int(rec.observation_window.total_seconds()),
                user_action=rec.user_action.value,
                shown_at=rec.shown_at,
                sent_at=rec.sent_at,
                model_version=rec.model_version,
            )
        )
        await self.s.flush()
        return rec.id

    async def awaiting_outcome(
        self, scope: Scope, now: datetime | None = None
    ) -> list[StrategyRecordRow]:
        """Sent strategies whose observation window has closed and which have
        no outcome row yet."""
        now = now or datetime.now(UTC)
        q = (
            select(StrategyRecordRow)
            .outerjoin(
                StrategyOutcomeRow,
                StrategyOutcomeRow.strategy_id == StrategyRecordRow.id,
            )
            .where(
                StrategyRecordRow.relationship_id == scope.relationship_id,
                StrategyRecordRow.sent_at.isnot(None),
                StrategyOutcomeRow.id.is_(None),
            )
        )
        rows = list((await self.s.execute(q)).scalars())
        return [
            r
            for r in rows
            if r.sent_at and r.sent_at + timedelta(seconds=r.observation_window_s) <= now
        ]

    async def add_outcome(self, scope: Scope, outcome: StrategyOutcome) -> UUID:
        _guard(scope, outcome.relationship_id)
        strategy = await self.s.get(StrategyRecordRow, outcome.strategy_id)
        if strategy is None:
            raise ScopeViolation("outcome references an unavailable strategy")
        _guard(scope, strategy.relationship_id)
        evidence_ids = list(outcome.evidence_interaction_ids)
        if evidence_ids:
            evidence = list(
                (
                    await self.s.execute(
                        select(InteractionRow).where(InteractionRow.id.in_(evidence_ids))
                    )
                ).scalars()
            )
            if len({row.id for row in evidence}) != len(set(evidence_ids)) or any(
                row.relationship_id != scope.relationship_id for row in evidence
            ):
                raise ScopeViolation("outcome evidence interactions cross scope")
        self.s.add(
            StrategyOutcomeRow(
                id=outcome.id,
                strategy_id=outcome.strategy_id,
                relationship_id=outcome.relationship_id,
                observed=outcome.observed.model_dump(mode="json"),
                label=outcome.label.value,
                evidence_interaction_ids=evidence_ids,
                window_start=outcome.window_start,
                window_end=outcome.window_end,
                confounders=list(outcome.confounders),
                valid_for_learning=outcome.valid_for_learning,
                scorer_version=outcome.scorer_version,
            )
        )
        await self.s.flush()
        return outcome.id


class MemoryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def search(
        self, scope: Scope, embedding: list[float], k: int = 6
    ) -> list[MemoryChunkRow]:
        """Scope filter is inside the query, not applied afterwards.

        A post-filter would still have ranked another relationship's chunks
        into the top-k and thrown away the ones that mattered.
        """
        q = (
            select(MemoryChunkRow)
            .where(MemoryChunkRow.relationship_id == scope.relationship_id)
            .order_by(MemoryChunkRow.embedding.cosine_distance(embedding))
            .limit(k)
        )
        return list((await self.s.execute(q)).scalars())

    async def add(
        self,
        scope: Scope,
        kind: str,
        summary: str,
        embedding: list[float] | None = None,
        source_ids: list[UUID] | None = None,
        source_kind: str | None = None,
        sensitivity: str = "standard",
    ) -> UUID:
        sources = source_ids or []
        if sources and source_kind is None:
            raise ScopeViolation("memory source_ids require an explicit source_kind")
        source_models = {
            "interaction": InteractionRow,
            "signal": SignalRow,
            "state": RelationshipStateRow,
            "preference_evidence": PreferenceEvidenceRow,
            "strategy": StrategyRecordRow,
            "outcome": StrategyOutcomeRow,
            "consent": ConsentRecordRow,
        }
        if source_kind is not None and source_kind not in {*source_models, "message"}:
            raise ScopeViolation(f"unsupported memory source_kind: {source_kind}")
        if sources:
            if source_kind == "message":
                found = list(
                    (
                        await self.s.execute(
                            select(MessageRow, InteractionRow.relationship_id)
                            .join(
                                InteractionRow,
                                InteractionRow.id == MessageRow.interaction_id,
                            )
                            .where(MessageRow.id.in_(sources))
                        )
                    ).all()
                )
                valid = len({row.id for row, _ in found}) == len(set(sources)) and all(
                    rid == scope.relationship_id for _, rid in found
                )
            else:
                model = source_models[source_kind]
                found = list(
                    (
                        await self.s.execute(select(model).where(model.id.in_(sources)))
                    ).scalars()
                )
                valid = len({item.id for item in found}) == len(set(sources)) and all(
                    item.relationship_id == scope.relationship_id for item in found
                )
            if not valid:
                raise ScopeViolation("memory sources cross the relationship scope")
        row = MemoryChunkRow(
            relationship_id=scope.relationship_id,
            kind=kind,
            summary=summary,
            embedding=embedding,
            source_ids=sources,
            source_kind=source_kind,
            sensitivity=sensitivity,
        )
        self.s.add(row)
        await self.s.flush()
        return row.id
