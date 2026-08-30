"""Preference aggregation: evidence rows -> Beta posteriors.

Thin by design. The arithmetic lives in BetaEstimate and the isolation rules
live in PreferenceModel.build; this module only bridges storage rows to the
domain types and decides which user-level priors are allowed to apply.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from ..domain.schemas.preference import (
    Direction,
    PreferenceDomain,
    PreferenceEvidence,
    PreferenceModel,
    UserTacticPrior,
)
from ..domain.schemas.primitives import Provenance, Sensitivity

SCORER_VERSION = "preference/1.0.0"


def input_fingerprint(evidence_rows: list, priors: dict[str, UserTacticPrior]) -> str:
    """Stable identity of evidence and priors used to build a model."""
    payload = {
        "scorer_version": SCORER_VERSION,
        "evidence": sorted(
            (
                str(row.id),
                row.direction,
                row.strength,
                row.excluded_from_learning,
            )
            for row in evidence_rows
        ),
        "priors": {
            key: value.model_dump(mode="json") for key, value in sorted(priors.items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def hydrate_evidence(rows: list) -> list[PreferenceEvidence]:
    out: list[PreferenceEvidence] = []
    for r in rows:
        out.append(
            PreferenceEvidence(
                id=r.id,
                person_id=r.person_id,
                relationship_id=r.relationship_id,
                domain=PreferenceDomain(r.domain),
                key=r.key,
                direction=Direction(r.direction),
                strength=r.strength,
                provenance=Provenance(r.provenance),
                observed_at=r.observed_at,
                source_interaction_id=r.source_interaction_id,
                source_signal_ids=tuple(r.source_signal_ids or ()),
                source_strategy_outcome_id=r.source_strategy_outcome_id,
                stage_at_time=r.stage_at_time,
                channel=r.channel,
                in_group_setting=r.in_group_setting,
                known_confounders=tuple(r.known_confounders or ()),
                excluded_from_learning=r.excluded_from_learning,
                sensitivity=Sensitivity(r.sensitivity),
            )
        )
    return out


def applicable_priors(
    priors: dict[str, UserTacticPrior], evidence: list[PreferenceEvidence]
) -> dict[str, UserTacticPrior]:
    """A user-level prior applies only where this person is still unknown.

    Once there is real evidence about her, the prior has nothing left to add
    and every reason to mislead -- so it is dropped rather than blended.
    """
    seen: dict[str, int] = {}
    for e in evidence:
        if e.counts_toward_learning:
            seen[e.key] = seen.get(e.key, 0) + 1
    return {k: p for k, p in priors.items() if seen.get(k, 0) < 3}


def rebuild(
    person_id: UUID,
    relationship_id: UUID,
    evidence_rows: list,
    priors: dict[str, UserTacticPrior] | None = None,
    version: int = 1,
) -> PreferenceModel:
    evidence = hydrate_evidence(evidence_rows)
    use = applicable_priors(priors or {}, evidence)
    return PreferenceModel.build(
        person_id=person_id,
        evidence=evidence,
        scorer_version=SCORER_VERSION,
        relationship_id=relationship_id,
        version=version,
        priors=use,
    )
