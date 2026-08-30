# The five locked schemas

Source of truth is the Python. The SQL in `storage/postgres/001_core.sql`
mirrors it. Changing either without the other is a bug.

| # | Schema | Layer | Mutability |
|---|--------|-------|-----------|
| 1 | `PersonProfile` (+ `VoiceProfile`) | derived | versioned snapshot |
| 2 | `RelationshipState` | derived | versioned snapshot |
| 3 | `Interaction` (+ `Message`, `Signal`) | observed | append-only |
| 4 | `PreferenceEvidence` (+ `PreferenceModel`) | observed / derived | append-only / recomputable |
| 5 | `StrategyRecord` + `StrategyOutcome` | decision / observed | append-only, two-phase |

## The seven invariants

These are enforced by validators and CHECK constraints, not by convention.
They exist because each one maps to a specific way this product fails.

1. **No inference without evidence.** An `Assessment` with
   `provenance=MODEL_INFERENCE` and zero `EvidenceRef` will not construct.

2. **Confidence is capped by evidence count.** One message supports at most
   0.45 confidence; 0.85 needs eight. Directly targets *False Positive
   Attraction Rate* (doc §59) — the agent cannot say "she likes you" from
   one fast reply, because the type will not hold the number.

3. **Repeated beliefs are Beta posteriors with a visible n.** `BetaEstimate`
   replaces bare floats. Below n=3 the only permitted output string is
   `"unknown"`. Replaces doc §17's `response_score: 0.88, confidence: 0.82`,
   which cannot distinguish 2 samples from 40.

4. **Protected namespaces cannot be inferred.** `sexual.*`, `kink.*`,
   `body.*`, `health.*`, `consent.*` accept only `USER_STATED` /
   `TARGET_STATED`. This is *Preference Hallucination Rate* (doc §59) made
   structurally impossible: "she is assertive at work" has no path to
   "she likely prefers to submit", because the write is rejected.

5. **Consent has no `likely`.** `ConsentStatus` is
   `confirmed | declined | hard_limit | unknown`, and `ConsentRecord`
   rejects even `OBSERVED_BEHAVIOR`. A preference may be probabilistic;
   consent may not. Attraction is stored in a different object entirely so
   there is no field for it to leak through (doc §39).

6. **Prediction is written before outcome, and frozen.** `StrategyRecord`
   holds `predicted_positive` and is immutable; `StrategyOutcome` is a
   separate later row. Without this ordering the calibration numbers are
   decoration, and the outcome dataset — barrier #4 in doc §68 — is worth
   nothing.

7. **A boundary signal outranks every engagement metric.** If
   `discomfort_or_boundary` is true the outcome label must be `negative`,
   however fast she replied. Prevents the learner from discovering that
   pressure "works".

## Isolation: three scopes, not one

Every table is either user-scoped or dyad-scoped. Nothing is "sort of both",
because that is where person A's data reaches person B.

| Scope | Contents | Rule |
|---|---|---|
| user | self `PersonProfile`, `VoiceProfile`, `UserTacticPrior` | shared on purpose |
| dyad (`relationship_id`) | target profile, state, interactions, signals, preferences, strategies, memory, vault, consent | hard isolated |
| global | skills, knowledge | shared, contains no person data |

Enforced at three layers, because the application layer will eventually
forget a `WHERE` clause exactly once:

1. `relationship_id` is NOT NULL on every dyad-scoped table. A nullable scope
   means "global", and a vector search that drops the filter will surface
   A's inside joke as context for B.
2. Postgres RLS with `app.relationship_id`. A forgotten filter returns
   nothing instead of returning someone else.
3. `CriticScores.cross_dyad_leak_risk` gates hard at 0.05, scored by exact
   match against other dyads' memory rather than by model judgement.

**The one sanctioned channel between relationships** is `UserTacticPrior`:
Beta parameters for a tactic key, no person, no text, no context. It seeds
`prior_alpha/prior_beta` so a new relationship starts from the user's own
base rate. It requires k>=3 distinct relationships, never applies to
protected domains, and cannot lift a belief out of `UNKNOWN` on its own --
a prior may shift the mean, but only person-level evidence earns the right
to claim knowledge of that person.

## Four deliberate departures from the architecture doc

- **`Stage` is UI-only.** Kept (doc §19) but marked `stage_is_derived`, and
  no strategy code may branch on it. Otherwise the eight-stage ladder
  quietly becomes the model again and the multi-dimensional state is
  decorative.

- **`PreferenceEvidence` split from `PreferenceModel`.** The doc treats
  preference as one object. Atomic evidence must be separable from the
  posterior so the scorer can change without corrupting history.

- **`health` is independent of `attraction`.** Neither feeds the other, so
  "chemistry up, health down" (doc §30) is representable. If health were a
  component of a single relationship score it would be arithmetically
  invisible.

- **Third-party profiles expire by default.** `retention_until` is NOT NULL
  for anyone who is not the user. The doc's §45 vault covers secrecy but not
  retention, and the legal exposure here is not the sexual data — it is
  holding an inferred psychological profile of a real person who never
  agreed to be modelled.

## Not in scope for v1

Runtime, skills, prompts, the Humanizer, and the model provider all sit
above this layer and are meant to be replaceable. The point of locking these
five first is that they should survive replacing all of it.
