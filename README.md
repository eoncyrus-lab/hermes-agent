# Sonder

*sonder* — the realisation that each passer-by has a life as vivid and complex
as your own.

A personal relationship intelligence agent. It reads conversations you show
it, maintains an evidence-backed model of one relationship at a time, suggests
things you might say in your own voice, and learns from what actually happened
afterwards.

It is not a pickup tool. The difference is architectural, not tonal: the
things such a tool would need to do — infer what someone wants from what they
are like, assert confidence it has not earned, optimise engagement past
someone's discomfort, carry what worked on one person over to another — are
rejected by the schema layer, not discouraged by the prompt.

## What is here

```
sonder/domain/schemas/   the five locked schemas + their invariants
sonder/llm/              provider abstraction: Anthropic, any OpenAI-compatible
sonder/storage/          scoped sessions, repositories, SQLAlchemy mapping
sonder/analyzer/         screenshot -> messages -> features -> signals
sonder/state/            signals -> RelationshipState; trends; preferences
sonder/agent/            skills, context, strategy, generator, humanizer, critic
sonder/learning/         outcome scoring, calibration, cross-person priors
sonder/api|bot|worker/   FastAPI, Telegram, arq pipeline
skills/                  eight domain skills (knowledge, never people)
migrations/001_core.sql  the schema, with RLS and append-only enforcement
evals/                   calibration, leakage and hallucination suites
tests/                   invariant, calibration, integration and isolation tests
```

## The invariants

Enforced by pydantic validators AND Postgres constraints, so neither layer is
the only thing standing between a user and a bad claim.

1. **No inference without evidence.** A model-inferred `Assessment` with zero
   evidence refs will not construct.
2. **Confidence is capped by evidence count.** One observation supports at
   most 0.45. Eight consistent ones support 0.85. Evidence identity is
   `(kind, ref_id)`: copied rows cannot inflate the count, and the ceiling
   table carries an explicit policy version for later calibration.
3. **Repeated beliefs are Beta posteriors with a visible n.** Below n=3 the
   only permitted output is `unknown`.
4. **Protected namespaces cannot be inferred.** `sexual.*`, `kink.*`,
   `body.*`, `health.*`, `consent.*` accept only explicit statements. "She is
   assertive at work" has no path to a claim about what she wants in bed.
5. **Consent has no probabilistic state.** `confirmed | declined |
   hard_limit | unknown`. Attraction lives in a different object entirely.
6. **Prediction is written before outcome, and frozen.** Otherwise the
   calibration numbers are decoration.
7. **A discomfort signal outranks every engagement metric.** However fast she
   replied.
8. **Relationships are isolated.** Three independent layers, below.

## Isolation

Every table is either user-scoped or dyad-scoped; nothing is both.

| Scope | Contents | Rule |
|---|---|---|
| user | self profile, voice, tactic priors | shared on purpose |
| dyad | target profile, state, interactions, signals, preferences, strategies, memory, consent | hard isolated |
| global | skills | shared, contains no person data |

Enforced three times over, because the application layer will forget a `WHERE`
clause exactly once:

1. `relationship_id NOT NULL` on every dyad-scoped table, and a `Scope`
   object that repositories require as their first argument
2. Postgres RLS keyed on `app.relationship_id` — verified: a query that
   drops its filter returns zero rows, and an unscoped session sees nothing
3. `CriticScores.cross_dyad_leak_risk`, a mechanical match against other
   relationships' callbacks, gating hard at 0.05

The one sanctioned channel between relationships is `UserTacticPrior`: Beta
parameters for a tactic key, no person, no text, no context, minimum three
relationships, never for protected domains, and unable on its own to lift a
belief out of `unknown`.

## Hot path and slow path

Reply suggestions do **not** run an agent loop. State and preferences are
precomputed, so the path is generate → humanize → critique → rank, in a few
seconds, because the user is standing in a chat window.

Analysis, review and strategy do load skills and reason at length, because the
user asked for a considered answer and twenty seconds is fine.

Conflating the two is what makes assistants either slow or shallow.

## Providers

Call sites name a `Task` and, where it matters, a `Track` — never a model and
never a vendor. Extraction runs cheap, the Humanizer runs on whatever writes
best, and a `ContentPolicyRefusal` fails over rather than dead-ending. Any
OpenAI-compatible endpoint works — OpenRouter, Together, vLLM, Ollama, local.

### Adding a vendor

MiniMax, DeepSeek, OpenRouter and OpenCode Zen each need one API key; the
profile supplies the endpoint and protocol. A private gateway or a second
account is a `SONDER_LLM_PROVIDERS` entry naming an existing profile.

Routes are ordered chains, keyed `"<task>.<track>"`. The first hop this
deployment has configured serves; a policy refusal moves to the next:

```
SONDER_DEEPSEEK_API_KEY=...
SONDER_OPENROUTER_API_KEY=...
SONDER_LLM_ROUTES={"generate.standard":[
  {"provider":"deepseek","model":"deepseek-chat"},
  {"provider":"openrouter","model":"openrouter/free"}]}
```

Two rules the router will not let a deployment bend:

**Every third-party vendor is permissive.** An aggregator does not guarantee
which weights served a request, and a reseller's safety tuning cannot be
inspected from here. So none of the four may serve `EXTRACT` or `CRITIC` — not
as a primary, not as a failover hop. A chain that lists one for those tasks is
a startup error, not a silently skipped hop.

**Third-party vendors carry no default model.** Free tiers and `:free`
variants churn, so an inherited default would quietly send `claude-opus-5` to
DeepSeek, or keep pointing at a model that stopped existing. Name it in a
chain or the router refuses the route.

Check a deployment before it serves traffic:

```bash
sonder llm validate                          # resolve and guard every route
sonder llm smoke --provider deepseek --model deepseek-chat
```

`validate` resolves all twelve task × track routes, prints each provider's
trust level and each chain's failover order, and exits non-zero on an unsafe
or incoherent configuration. `smoke` sends one real eight-token completion.

### The intimate track

A general-purpose hosted model hedges or declines on frank discussion of sex
between consenting adults, which makes it unusable for that part of the
product. `Track.INTIMATE` routes **generation and rewriting** to a model the
operator hosts themselves.

Signal extraction and the Critic never follow the track, in either direction,
and `Router.resolve` raises `UnsafeRouting` rather than allowing it — failover
included. The reason is specific: those tasks exist to notice discomfort,
boundaries and pressure, and a model tuned away from refusing is also tuned
away from noticing. A compliant model scoring `boundary_risk` returns low
numbers for everything, the Critic starts passing candidates it should block,
and nothing in the output looks wrong.

The track is a routing hint and relaxes no domain rule. Protected preference
namespaces still cannot be inferred, consent still has no probabilistic state,
and every candidate still passes the Critic.

Use a leading `/intimate` to select this track explicitly when a request does
not contain an obvious classifier keyword. The prefix is stripped before the
request reaches a model. Local and custom provider endpoints are always
treated as untrusted/permissive for routing purposes; the legacy
`local_is_permissive` setting cannot promote one into extraction or Critic
work. Verify a configured vLLM/Ollama-compatible endpoint with
`sonder llm smoke-local`, which checks both `/models` and a real chat
completion.

### Calibration is offline and advisory

`CalibrationObservationV1` joins a frozen prediction to its later outcome and
records the bucket, model version and confidence-policy version. The offline
report groups on all three, reports sample count, Brier score, bias, intervals
and excluded-data counts. Empty groups, synthetic observations, indeterminate
outcomes and groups below the real-sample floor are labelled
`INSUFFICIENT_REAL_DATA`; they cannot produce metrics.

Recommendations are limited to `hold`, `tighten`, or
`eligible_for_manual_review`. The reporter is a pure, read-only module and has
no path to runtime settings, so it never changes a ceiling automatically.

## Running it

```bash
cp .env.example .env         # set the five role passwords and a provider key
docker compose up -d db redis
docker compose up api worker
docker compose --profile bot up bot

# locally
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
./scripts/check.sh
```

Nothing in the tree is a credential. The migrations create every service role
without a password — which under md5/scram means the role cannot authenticate
at all — and `scripts/db-roles.sh` sets them from the environment on each
`up`, which is also the rotation path. Compose refuses to start if one is
unset rather than falling back to a default. Postgres and Redis publish no
host port; the API publishes only on loopback.

## Database identities

Six roles, and the split is the design:

| role | reach | held by | BYPASSRLS |
|---|---|---|---|
| `sonder_identity` | one bearer-token RPC. No table privileges at all. | api | no |
| `sonder_bot` | one Telegram RPC. No table privileges at all. | bot | no |
| `sonder_app` | one relationship, of one verified user | api, bot | no |
| `sonder_erase` | every relationship of one verified user | api | no |
| `sonder_worker` | one pinned relationship, no user identity | worker | no |
| `sonder_maint` | everything. Two scheduled jobs. | worker | **yes** |

`sonder_bot` exists separately from `sonder_identity` because the two RPCs are
not equally hard to guess. `auth_bearer` needs a 256-bit token digest;
`auth_telegram` takes a ten-digit Telegram user id, often a public one — so
whatever can call it can walk the id space and trade a hit for a working
claim. Only the bot has a reason to, and only the bot gets it.

The load-bearing part is what a session has to prove. `app.user_id` is a
claim, not a credential: the policies resolve the current user through
`app_current_user_id()`, which returns a row only when the session also
carries `app.identity_claim` — the digest of a live, unrevoked bearer token
for that same user. A handler that sets `app.user_id` to someone else's id
gets zero rows, and no service role can read the token table to forge one.

The dyad policies ask the same question. Pinning `app.relationship_id` used to
be sufficient on its own, so knowing an id was enough to read it; the policy
now also requires that relationship to belong to the verified user.

A queued job is the one path with no bearer token to present, so
`sonder_worker`'s policies can only enforce the pinned relationship. The other
half — that the job's user id actually owns it — is checked in
`worker.tasks.authorize`, which raises rather than proceeding.

None of these DSNs falls back to another: a missing one is a startup error,
not a quiet demotion to the app role. That matters because the demotion is
invisible — the worker would build preference models with empty
cross-relationship priors, outcome scoring would see no relationships, and
erasure would report every count as zero while the cascade removed the data.

`sonder token create|list|revoke` reads and writes `api_tokens`, which only
`sonder_maint` may touch, so the operator CLI needs
`SONDER_MAINTENANCE_DATABASE_URL` set. It is an operator tool, not a service.

One residual worth knowing: the claim is the token digest, and it travels
through `app.identity_claim` on every session. A Postgres configured to log
statements or parameters will therefore write replayable claims to its logs,
so don't enable `log_statement = 'all'` against this database.

The check entrypoint runs lint, formatting, unit tests, evals, then starts a
temporary `pgvector/pgvector:pg16` database and runs the real Postgres tests.
Those verify the installed extension version, `vector(1536)` schema and cosine
index, 1536-dimensional writes and search order, wrong-dimension rejection,
and cross-relationship isolation. Missing Docker or an unavailable daemon is
an explicit failure, never a green skipped run. CI calls the same script.

### Two database roles, and the split is the point

```sql
CREATE ROLE sonder_app   LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '...';
CREATE ROLE sonder_maint LOGIN NOSUPERUSER BYPASSRLS   PASSWORD '...';
GRANT USAGE ON SCHEMA public TO sonder_app, sonder_maint;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO sonder_app, sonder_maint;
```

`sonder_app` serves every request and cannot bypass RLS. `sonder_maint`
(`SONDER_MAINTENANCE_DATABASE_URL`) is used only by erasure, outcome scoring
and artifact purging, which legitimately span relationships — and those
callers do their own ownership checks, because RLS is not covering them.

Running the app as the table owner or a superuser silently disables every
policy above. The default Compose deployment creates these roles during
database initialization and pins API/worker traffic to `sonder_app`, with the
separate maintenance DSN pinned to `sonder_maint`. For an existing database,
run `migrations/002_security_boundaries.sql` as a PostgreSQL superuser before
switching DSNs; it aborts if any relationship currently points at a profile
owned by another user, or if any derived row mixes relationship ownership.

Then run `migrations/003_identity_and_idempotency.sql`, as the same superuser.
It is not optional for an existing deployment: `identity_factory` has no
fallback DSN, so code upgraded without it loses authentication outright. Both
files are rerunnable in any order, and application, maintenance and
authentication roles are all rejected as migration identities.

Cross-relationship text comparison in the leak critic is disabled. A shared
application role plus caller-set relationship scope cannot safely authenticate
tenant ownership for a cross-dyad text reader. The critic degrades to its other
boundary checks instead; there is no cross-dyad reader role, DSN, or executable
`SECURITY DEFINER` function.

## Access

Multi-user. Bearer tokens stored as SHA-256 digests, so a database dump does
not also hand over live sessions. Identity comes from `current_user_id` and
nowhere else; every route naming a relationship or person passes an ownership
check first, because RLS scopes a session to one relationship without saying
whose it is. A cross-account request gets 404, not 403 — whether someone
else's relationship exists is itself not disclosed.

Telegram identity resolves through `telegram_accounts`; an unlinked chat gets
nothing, and the in-memory person selection is re-verified against the
database on every message.

Bearer tokens no longer require manual SQL. Use `sonder token create --user
<UUID>`, `sonder token list --user <UUID>`, and `sonder token revoke --user
<UUID> <TOKEN_ID>`. Plaintext is printed only at creation; listing never
reveals token digests.

Telegram `/list` shows relationship UUIDs. `/who <relationship UUID>` is the
unambiguous selector; exact display names remain a convenience only when
unique, while duplicate names return choices instead of selecting by accident.
Selections are stored by relationship UUID and revalidated before use.

## Erasure

The control that makes modelling a real person acceptable here, so it is
treated as a security boundary rather than a convenience.

| | |
|---|---|
| `GET /person/{id}` | everything held about them, including which traits were inferred — read this before deciding |
| `DELETE /person/{id}` | the person and everything ever derived from them; verifies afterwards and returns 500 rather than reporting a partial success |
| `DELETE /relationship/{id}` | the relationship only, keeping the person record |
| `GET /residue` | profiles with no live relationship, or past retention |

Deleting the relationship alone leaves the inferred profile behind, which is
the wrong half to keep — hence the separate person-level erasure and the
residue sweep.

## Channels

Telegram, plus an iOS share-sheet shortcut posting screenshots to `/ingest`.

**Not WeChat via a bot protocol**, though that is where the conversations are:
driving a personal account through an unofficial protocol risks the account.
WeChat is a source of screenshots the user chooses to share. That constraint
turned out to be the better product — "here is something I am showing you" is
a different relationship with a tool than "it reads everything I say".

## Deliberately not built

`sexual-skills`, `kink` and the simulation engine (doc §32–38, §53). The
`intimacy` skill and its track are in; the narrower technique and kink
modules are not, because they depend on preference and boundary state being
trustworthy first, and that needs real usage to calibrate. Their guardrails
already exist and are tested: protected namespaces, consent typing and the
vault tier all refuse writes today.
