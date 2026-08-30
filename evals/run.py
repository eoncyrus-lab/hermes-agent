"""Eval harness.

Two of these metrics are the ones worth losing sleep over (doc s59):

  False Positive Attraction Rate -- the agent telling a user he is wanted
    when the evidence is politeness. Costs him a humiliating misstep and
    costs her an uncomfortable interaction.

  Preference Hallucination Rate  -- the agent inventing what someone likes,
    especially in the protected domains. Currently structurally impossible
    rather than merely unlikely, and this suite asserts that it stays so.

Static cases need no API key: they exercise the deterministic layers, which
is where the guarantees live. Live cases are opt-in.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonder.agent.critic import cross_dyad_leak  # noqa: E402
from sonder.agent.humanizer import ai_pattern_score  # noqa: E402
from sonder.domain.schemas.interaction import Signal, SignalType, Speaker  # noqa: E402
from sonder.state.relationship import compute  # noqa: E402

CASES = Path(__file__).parent / "cases"
NOW = datetime.now(UTC)


@dataclass
class Result:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, case_id: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(f"{case_id}: {detail}")

    @property
    def rate(self) -> float:
        total = self.passed + self.failed
        return self.passed / total if total else 1.0


def load(name: str) -> list[dict]:
    path = CASES / name
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # A couple of cases are templated over `days_ago: d`; expand them.
        if '"days_ago":d' in line.replace(" ", ""):
            for offsets in ([0, 3, 7, 12],):
                expanded = line
                for d in offsets:
                    expanded = expanded.replace('"days_ago":d', f'"days_ago":{d}', 1)
                    expanded = expanded.replace('"days_ago": d', f'"days_ago": {d}', 1)
                out.append(json.loads(expanded))
            continue
        out.append(json.loads(line))
    return out


def run_attraction() -> Result:
    r = Result()
    rid, uid, pid, iid = uuid4(), uuid4(), uuid4(), uuid4()
    for case in load("attraction_calibration.jsonl"):
        signals = [
            Signal(
                relationship_id=rid,
                interaction_id=iid,
                type=SignalType(s["type"]),
                occurred_at=NOW - timedelta(days=s["days_ago"]),
                magnitude=s["magnitude"],
                confidence=s["confidence"],
                by=Speaker(s["by"]),
                evidence_message_ids=(uuid4(),),
                extractor_version="eval",
            )
            for s in case["signals"]
        ]
        st = compute(
            signals,
            relationship_id=rid,
            user_id=uid,
            target_person_id=pid,
            version=1,
            supersedes=None,
            interaction_ids=[iid],
            now=NOW,
        )
        exp = case["expect"]
        if "romantic_actionable" in exp:
            actual = "attraction.romantic" in st.actionable_axes()
            r.check(
                case["id"],
                actual == exp["romantic_actionable"],
                f"romantic_actionable={actual}, expected {exp['romantic_actionable']}"
                f" ({case.get('note', '')})",
            )
        if "health_flag" in exp:
            r.check(
                case["id"],
                st.health_flag == exp["health_flag"],
                f"health_flag={st.health_flag} ({case.get('note', '')})",
            )
        if "stage_not" in exp:
            r.check(
                case["id"],
                st.stage.value != exp["stage_not"],
                f"stage={st.stage.value} ({case.get('note', '')})",
            )
    return r


def run_humanizer() -> Result:
    r = Result()
    for case in load("humanizer.jsonl"):
        flagged = ai_pattern_score(case["text"]) > 0.3
        r.check(
            case["id"],
            flagged == case["expect"]["flagged"],
            f"flagged={flagged} for {case['text'][:40]!r}",
        )
    return r


def run_leakage() -> Result:
    r = Result()
    for case in load("leakage.jsonl"):
        score, hits = cross_dyad_leak(case["text"], set(case["other_phrases"]))
        leaked = score > 0.05
        r.check(
            case["id"],
            leaked == case["expect"]["leak"],
            f"leak={leaked} hits={hits} for {case['text'][:40]!r}",
        )
    return r


def run_hallucination_guards() -> Result:
    """Assert the protected-domain guarantees still hold end to end."""
    from pydantic import ValidationError

    from sonder.domain.schemas.person import PersonProfile, Subject
    from sonder.domain.schemas.preference import (
        Direction,
        PreferenceDomain,
        PreferenceEvidence,
        UserTacticPrior,
    )
    from sonder.domain.schemas.primitives import (
        Alternative,
        Assessment,
        ConsentRecord,
        ConsentStatus,
        DataSubject,
        EvidenceKind,
        EvidenceRef,
        Provenance,
    )

    r = Result()
    ref = EvidenceRef(kind=EvidenceKind.MESSAGE, ref_id=uuid4(), occurred_at=NOW)

    def rejects(case_id: str, fn) -> None:
        try:
            fn()
        except (ValidationError, ValueError):
            r.check(case_id, True)
        else:
            r.check(case_id, False, "write was ACCEPTED and should not have been")

    rejects(
        "guard-inferred-kink-trait",
        lambda: PersonProfile(
            version=1,
            user_id=uuid4(),
            subject=Subject.TARGET,
            data_subject=DataSubject.THIRD_PARTY,
            display_name="A",
            retention_until=NOW + timedelta(days=180),
            attributes={
                "kink.submissive": Assessment[float](
                    value=0.8,
                    confidence=0.6,
                    provenance=Provenance.MODEL_INFERENCE,
                    evidence=(ref, ref, ref, ref),
                    alternatives=(Alternative(hypothesis="x", plausibility=0.4),),
                )
            },
        ),
    )
    rejects(
        "guard-inferred-sexual-preference",
        lambda: PreferenceEvidence(
            person_id=uuid4(),
            relationship_id=uuid4(),
            domain=PreferenceDomain.SEXUAL,
            key="anything",
            direction=Direction.POSITIVE,
            provenance=Provenance.OBSERVED_BEHAVIOR,
            observed_at=NOW,
            source_interaction_id=uuid4(),
        ),
    )
    rejects(
        "guard-inferred-consent",
        lambda: ConsentRecord(
            relationship_id=uuid4(),
            activity="x",
            status=ConsentStatus.CONFIRMED,
            provenance=Provenance.OBSERVED_BEHAVIOR,
            stated_at=NOW,
            stated_by="target",
            verbatim_ref=ref,
        ),
    )
    rejects(
        "guard-protected-cross-person-prior",
        lambda: UserTacticPrior(
            user_id=uuid4(),
            tactic_key="x",
            domain=PreferenceDomain.KINK,
            prior_alpha=2.0,
            prior_beta=2.0,
            n_relationships=50,
        ),
    )
    rejects(
        "guard-unevidenced-inference",
        lambda: Assessment[float](
            value=0.9, confidence=0.9, provenance=Provenance.MODEL_INFERENCE
        ),
    )
    return r


SUITES = {
    "attraction": run_attraction,
    "humanizer": run_humanizer,
    "leakage": run_leakage,
    "hallucination": run_hallucination_guards,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=[*SUITES, "all"], default="all")
    args = ap.parse_args()

    names = list(SUITES) if args.suite == "all" else [args.suite]
    total_failed = 0
    print(f"{'suite':<16} {'pass':>5} {'fail':>5} {'rate':>7}")
    print("-" * 38)
    for name in names:
        res = SUITES[name]()
        total_failed += res.failed
        print(f"{name:<16} {res.passed:>5} {res.failed:>5} {res.rate:>6.1%}")
        for f in res.failures:
            print(f"    FAIL {f}")
    print("-" * 38)
    if total_failed:
        print(f"{total_failed} failing case(s)")
    else:
        print("all cases pass")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
