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
migrations/              ordered SQL upgrades (currently 001 through 006)
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

Enforced by two active layers, because the application layer will forget a
`WHERE` clause exactly once:

1. `relationship_id NOT NULL` on every dyad-scoped table, and a `Scope`
   object that repositories require as their first argument
2. Postgres RLS keyed on `app.relationship_id` — verified: a query that
   drops its filter returns zero rows, and an unscoped session sees nothing

The deterministic `cross_dyad_leak` helper remains dormant for a future
separately authenticated, user-scoped reader. Production does not fetch other
relationships' text, and `cross_dyad_leak_risk=0` currently means unavailable,
not “compared and clean.” The former database oracle was removed rather than
granting a shared service role cross-dyad text access.

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
best, and a refusal or exhausted provider error follows the configured chain.
OpenAI-compatible Chat Completions endpoints work; a provider configured for
the Responses protocol fails fast until that protocol is implemented.

### Credentials a user brings themselves

A deployment's keys serve everyone. A user can also store their own, which is
what lets somebody use a vendor the operator has no account with — or a model
they host, which is the only honest answer to a hosted model hedging on frank
adult conversation.

```bash
sonder credentials set --user <UUID> --provider my-vllm \
  --profile local --base-url http://127.0.0.1:8000/v1 --model hermes-4-70b --intimate
sonder credentials list --user <UUID>     # never prints a key
sonder credentials clear --user <UUID> --provider my-vllm
```

The key is read from the terminal, never from argv, which is in the shell
history and in `ps`. It is encrypted with `SONDER_VAULT_KEY` before it reaches
the database, so a dump of the database is not a dump of working API keys.

Two rules hold, and both are enforced in code rather than documented and hoped
for:

**A user credential is always permissive.** Whatever profile it names, the
official ones included — their key still points wherever they typed, and the
operator cannot verify it. So it can never serve `EXTRACT` or `CRITIC`, and a
route chain that tries is a startup error. Those two tasks are the operator's
guarantee to the person being modelled; somebody who can choose an endpoint
can choose one that has been talked out of its refusals, and that failure is
silent.

**A name the deployment already relies on for a safety task is not
overridable.** If the operator's `EXTRACT` runs on DeepSeek, a user's DeepSeek
key is stored and used for their other requests while that route keeps the
operator's credential. Replacing it would put their endpoint underneath a
safety route, and `validate_routes` would then fail every request they made.

An `--intimate` credential redirects **reason, generate and humanize** on the
intimate track to that endpoint, and nothing else. Extraction and criticism do
not follow the track in either direction, so they cannot be moved by it. The
first version of this redirected every `*.intimate` route; `router._guard`
refused it at startup with `UnsafeRouting`, which is the guard catching a real
mistake rather than a theoretical one.

### Adding a vendor

MiniMax, DeepSeek, OpenRouter and OpenCode Zen can coexist. Each needs its own
API key; the profile supplies the endpoint and Chat Completions protocol. A
private gateway or second account is a named `SONDER_LLM_PROVIDERS` entry.

| profile | default endpoint | model note |
|---|---|---|
| [MiniMax](https://api.minimax.io/v1) | `https://api.minimax.io/v1` | set a mainland-China base explicitly when required |
| [DeepSeek](https://api-docs.deepseek.com/) | `https://api.deepseek.com` | examples: `deepseek-v4-flash`, `deepseek-v4-pro` |
| [OpenRouter](https://openrouter.ai/docs/guides/routing/routers/free-router) | `https://openrouter.ai/api/v1` | `openrouter/free` and `:free` availability changes |
| [OpenCode Zen](https://dev.opencode.ai/docs/zen/) | `https://opencode.ai/zen/v1` | choose a currently listed chat-compatible free model |

Routes are ordered chains, keyed `"<task>.<track>"`. The first hop this
deployment has configured serves; a policy refusal moves to the next:

```
SONDER_DEEPSEEK_API_KEY=...
SONDER_OPENROUTER_API_KEY=...
SONDER_LLM_ROUTES={"generate.standard":[
  {"provider":"deepseek","model":"deepseek-v4-flash"},
  {"provider":"openrouter","model":"openrouter/free"}]}
```

Two rules the router will not let a deployment bend:

**Every third-party vendor is permissive by default.** An aggregator does not
guarantee which weights served a request, and a reseller's safety tuning cannot
be inspected from here. So MiniMax, OpenRouter and Zen may not serve `EXTRACT`
or `CRITIC` — not as a primary, not as a failover hop. A chain that lists one
for those tasks is a startup error, not a silently skipped hop.

> **This deployment departs from that default for DeepSeek.** It runs on a
> DeepSeek key alone, so the two safety-critical tasks have no first-party
> endpoint to fall back to. Rather than leave them unroutable, DeepSeek is
> marked safety-trusted in `sonder/llm/configuration.py` — in code, under
> review, and pinned by `tests/test_provider_registry.py`, not through an
> operator-settable switch (which is the thing the guard exists to prevent;
> `SONDER_SAFETY_TRUSTED_PROVIDERS` remains ignored). What that costs is stated
> at the `PROFILES` entry: the Critic is the last gate before a candidate
> reaches the user, and a model that declines less also notices less, silently.
> If a first-party key ever becomes available, restore the flag and route
> `EXTRACT` and `CRITIC` there.

**Third-party vendors carry no default model.** Free tiers and `:free`
variants churn, so an inherited default would quietly send `claude-opus-5` to
DeepSeek, or keep pointing at a model that stopped existing. Name it in a
chain or the router refuses the route.

Check a deployment before it serves traffic:

```bash
sonder llm validate                          # resolve and guard every route
sonder llm smoke provider deepseek --model deepseek-v4-flash
sonder llm smoke route generate --track standard
```

`validate` resolves all twelve task × track routes, reports enabled and
disabled providers, prints trust and failover order, and exits non-zero on an
unsafe or incoherent configuration. Provider smoke calls one named endpoint;
route smoke exercises normal resolution and ordered fallback. Neither assumes
every vendor implements `/models`.

### The intimate track

A general-purpose hosted model hedges or declines on frank discussion of sex
between consenting adults, which makes it unusable for that part of the
product. `Track.INTIMATE` routes **generation and rewriting** to a model the
operator hosts themselves.

> **This deployment does not do that yet.** `SONDER_LOCAL_BASE_URL` is unset
> and every `*.intimate` route in `.env` points at DeepSeek, so the intimate
> track currently has the exact failure mode this section describes — the
> hosted model may hedge or decline. The routing *machinery* is intact and the
> guard below still holds; only the self-hosted endpoint is missing. Point
> `reason.intimate` / `generate.intimate` / `humanize.intimate` at a vLLM or
> Ollama endpoint to get the intended behaviour.

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
request reaches a model. Local and custom provider endpoints are permanently
untrusted for safety-critical routing; configuration cannot promote one.
Verify a configured vLLM/Ollama-compatible endpoint with
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
cp .env.example .env
# Generate POSTGRES_PASSWORD, the seven service-role passwords, and the Redis
# password separately: openssl rand -hex 32
# Then set at least one provider API key and every other required secret.
docker compose up -d db redis
docker compose up api worker
docker compose --profile bot up bot

# locally
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
# Required outside Compose; there is no baked-in database owner credential.
export SONDER_DATABASE_URL=postgresql+asyncpg://sonder_app:...@localhost/sonder
./scripts/check.sh
```

### Running without building the application images

The three application images are not needed to run the stack. Docker can own
PostgreSQL alone, with the Python processes on the host:

```bash
docker compose up -d db          # fresh volume: 001..006 run in order
docker compose up db-roles       # assigns the six role passwords from .env
# redis is the Homebrew service on 127.0.0.1:6379, which has no password

./scripts/run-local.sh api       # uvicorn on 127.0.0.1:8000
./scripts/run-local.sh worker    # arq: ingest, analysis, the two cron jobs
./scripts/run-local.sh bot       # Telegram long polling
./scripts/run-local.sh cli llm validate
```

`docker-compose.override.yml` publishes the database on `127.0.0.1:5432` and
nothing else; delete that file to return to the containerised deployment. The
override exists because the base file deliberately does not publish a port —
"a published 5432 on a laptop is a published 5432 on whatever network that
laptop joins next" — and `docker-compose.test.yml` already publishes its test
database the same way. `scripts/run-local.sh` assembles the five DSNs from
`.env` because the six roles are not interchangeable: the API must never hold
the maintenance DSN, and the bot must hold `sonder_bot` rather than
`sonder_identity`.

PostgreSQL still has to be the container. The schema needs pgvector and the
six RLS roles that only `migrations/001..006` create, and a host PostgreSQL 14
has neither.

### The browser page

`GET /ui` serves a single-file chat page: pick a relationship, type, or attach
screenshots. It runs the same `orchestrator.handle` the bot does, through the
same ownership check and the same policies, so it reaches nothing the Telegram
bot could not. `POST /chat` exists for it because the API had split `handle`
into `/reply` and `/ask`, which forced a browser to guess the intent before the
classifier ran.

The page authenticates with a bearer token, and it does not have to be typed
in. Setting `SONDER_UI_TOKEN` makes `/ui` inject that token into the document
— but only when **both** the `Host` header names a loopback host **and** the
peer address is loopback.

The feature is shaped by a constraint worth knowing before changing it: the API
**cannot mint** a token. `api_tokens` is granted to `sonder_maint` alone and
the API is deliberately not given that DSN, while `sonder_identity` holds one
verification function and no issuance function. So "auto-login" can only mean
handing over a token an operator provisioned. Minting one per request would
also litter the table with live credentials.

Two checks guard the injection, and both are load-bearing. The client address
alone is not enough: a page on `evil.example` can point that name at 127.0.0.1
and make its fetches same-origin, and the request then genuinely arrives from
127.0.0.1 — only the `Host` header still says `evil.example`. The `Host` check
alone is not enough either, because a request proxied in from elsewhere can
forge it. `tests/test_ui_autologin.py` pins both.

What it does not defend against, stated plainly: **any local process that can
reach 127.0.0.1:8000 gets a working token for the configured account.** On a
single-user machine such a process can already read `.env` and the database,
so the token is not the weakest link — but that is a statement about a personal
machine, not one that would hold on a shared host. Clearing the setting
restores the typed-token behaviour and leaves the page otherwise unchanged.


Migrations run from `/docker-entrypoint-initdb.d` and therefore only execute
on an **empty** data directory. An existing volume keeps whatever schema it
was initialised with — `docker compose down -v` is what forces a clean
`001..006` pass, and `scripts/backup.sh` is what makes that safe to do.

Nothing in the tree is a credential. The migrations create every service role
without a password — which under md5/scram means the role cannot authenticate
at all — and `scripts/db-roles.sh` sets them from the environment on each
`up`, which is also the rotation path. Compose refuses to start if one is
unset rather than falling back to a default. Postgres and Redis publish no
host port; the API publishes only on loopback.

Use `openssl rand -hex 32` independently for every password embedded in a DSN.
Hex avoids URL delimiter characters. If an externally managed password uses
characters such as `:/@?#[]`, percent-encode the password component before
embedding it. Required Compose secrets are `POSTGRES_PASSWORD`, all seven
`SONDER_*_DB_PASSWORD` values, and `SONDER_REDIS_PASSWORD`; provider and vault
credentials are required when their corresponding features are enabled.

## Database identities

Seven roles, and the split is the design:

| role | reach | held by | BYPASSRLS |
|---|---|---|---|
| `sonder_identity` | one bearer-token RPC. No table privileges at all. | api | no |
| `sonder_bot` | one Telegram RPC. No table privileges at all. | bot | no |
| `sonder_wechat` | one WeChat RPC. No table privileges at all. | wechat | no |
| `sonder_app` | one relationship, of one verified user | api, bot, wechat | no |
| `sonder_erase` | every relationship of one verified user | api | no |
| `sonder_worker` | one pinned relationship, no user identity | worker | no |
| `sonder_maint` | everything. Two scheduled jobs. | worker | **yes** |

`sonder_bot` exists separately from `sonder_identity` because the two RPCs are
not equally hard to guess. `auth_bearer` needs a 256-bit token digest;
`auth_telegram` takes a ten-digit Telegram user id, often a public one — so
whatever can call it can walk the id space and trade a hit for a working
claim. Only the bot has a reason to, and only the bot gets it.

`sonder_wechat` is separate from `sonder_bot` for the same reason applied one
step further: a WeChat id is an opaque `…@im.wechat` handle rather than a
guessable integer, but the two RPCs still differ in how much damage a holder
can do, and a process should carry the narrowest credential that does its job.
A compromised WeChat bridge must not also mint Telegram claims. That is tested
rather than asserted: `tests/test_identity_boundary.py` connects as
`sonder_wechat` and checks it can execute `auth_wechat`, cannot execute
`auth_telegram`, and cannot read `wechat_accounts`.

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

### Migration and role installation

Apply migrations as an administrative migration identity, in numeric order:

1. `001_core.sql` creates the schema, base constraints, RLS and service roles
   without login passwords.
2. `002_security_boundaries.sql` tightens ownership constraints and removes
   the unsafe cross-dyad text oracle.
3. `003_identity_and_idempotency.sql` installs verified identity RPCs,
   least-privilege role grants and replay-safe ingest primitives.
4. `004_integrity_and_idempotency.sql` adds the remaining composite ownership
   references and idempotency constraints after fail-closed preflight checks.
5. `005_vault_scope_and_version_keys.sql` scopes vault records and strengthens
   version-chain ownership keys after fail-closed preflight checks.
6. `006_vault_ownership_and_cleanup.sql` binds relationship-scoped vault rows
   to the same owning user and removes superseded integrity objects.
7. `007_wechat_channel.sql` adds the WeChat identity mapping, its
   `auth_wechat` RPC and the `sonder_wechat` role, after a superuser check.
8. `008_user_llm_credentials.sql` adds per-user model credentials: encrypted
   at rest, row-secured like the vault, and deliberately unreachable by the
   worker so extraction cannot follow a user's endpoint.

Do not run the application as the table owner or a superuser: either silently
defeats RLS. After migrations, `scripts/db-roles.sh` assigns independently
generated passwords to the seven service identities. Existing installations
must apply all eight files in order before switching application code or DSNs;
preflight failures require repairing the reported inconsistent rows before
retrying. Application, identity, worker and maintenance service roles are not
migration identities.

**Apply them in order, and do not replay an early one on its own.** A later
file extends something an earlier one owns: `app_current_user_id()` learns the
`w` claim kind in 007, while 003 still contains the two-kind version — so
replaying 003 against a live database silently disables WeChat authentication.
Every `w` claim then resolves to no user and reads zero rows, with no error
raised anywhere. Since `004`..`007` are each idempotent, the repair is to
re-apply them in order. `tests/test_identity_boundary.py` does exactly that
after the one test that rewinds the history on purpose, which is how the
hazard was found rather than guessed at.

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

Telegram, an iOS share-sheet shortcut posting screenshots to `/ingest`, and —
since Tencent opened it officially — WeChat itself.

**The WeChat channel replaced an earlier refusal, and the refusal was right
for as long as it was true.** The old text here read: *not WeChat via a bot
protocol, though that is where the conversations are: driving a personal
account through an unofficial protocol risks the account.* That reasoning
applied to WeChatPadPro and PC-client hooks, which are account-risking reverse
engineering. Tencent now ships **ClawBot**, an official bot channel built on
the `ilink` protocol (`ilinkai.weixin.qq.com`, npm scope `@tencent-weixin`),
with published terms of use. `sonder/wechat/` speaks that protocol directly —
HTTP/JSON, no Node and no OpenClaw gateway required.

The second half of the old objection — *"it reads everything I say" is a
different relationship with a tool than "here is something I am showing you"* —
turned out not to apply either, and that is worth stating precisely because it
is the part that would have mattered. The channel is a **bot account you
message**, not your personal account being driven. The official client drops
any message from a sender that has not paired with it
(`dmPolicy: "pairing"`, checked in `process-message.ts`), so what reaches
sonder is what somebody deliberately sent to the bot. The product stance is
unchanged: you hand it conversations, it does not harvest them.

Pairing is a QR scan, and the credential it returns lives in `.env` beside
`SONDER_TELEGRAM_TOKEN`:

```bash
./scripts/run-local.sh cli wechat login    # prints a QR, stores the token
./scripts/run-local.sh cli wechat status   # paired or not; never prints it
```

`scripts/run-local.sh` is the form that always works, because it assembles the
DSNs from `.env` first. A checkout whose editable install has gone stale has no
`sonder` console script at all — `pip install -e .` restores it — and every
command except `wechat login` needs `SONDER_DATABASE_URL` set before it will
start, which is what the wrapper is doing for you.

The channel accepts text and forwarded pictures. A forwarded screenshot goes
through `ingest_screenshots` unchanged — the same vision call, the same
evidence refs, the same artifact TTL as a picture posted to `/ingest` — because
where a picture came from is not something the observation should depend on.
Media on WeChat's CDN is AES-128-ECB encrypted and the key travels in the
message; `sonder/wechat/media.py` handles both key encodings the API emits and
the two URL shapes, and `tests/test_wechat_media.py` covers it against a mocked
CDN. What that test cannot cover is the real CDN changing its object format.

**Why not the vault.** The plan for this channel said the bot token would live
in `vault_records`, and it does not. That table holds sensitive material *about
a person* — every value of its `provenance` enum is a statement by somebody —
and it is scoped by `relationship_id`. A channel credential is an operator
secret, which is what `.env` already holds for Telegram. The runner's role
settles it: `sonder_wechat` has no table privileges at all, so reading the
vault would mean granting it some, and "one RPC and nothing else" is a property
worth more than putting one string in a different file.

Two boundaries worth knowing before relying on it. Tencent's terms reserve the
right to throttle, filter, block or withdraw the channel (clauses 4.7 and
7.2), so it cannot be the only way in — Telegram stays. And `bot_type=3` is
hard-coded in the official client with no published meaning, so what account
type it selects is something the first real pairing has to confirm.

### One implementation, three surfaces

Telegram, WeChat and the browser page offer the same product, so the parts
that are the same live in `sonder/channels.py`: which relationship a chat is
about, how a name or a number resolves to one, and how a result is worded.
What stays with the channel is the outcome affordance — Telegram presses a
button, WeChat types a digit — because that is a difference in transport, not
in product.

The shape of a reply is decided once, and it leads with the sentence. The
first version put the objective, the rationale, the milestone, the notes and
the alternatives in the same message: measured against a real suggestion that
was 307 characters, of which 15 were the thing the user came for. Everything
else is now one question away — "为什么", a button, or a disclosure — and it
is rendered by `channels.render_detail` rather than by each front end. The page
used to hold its own copies of the objective and tone label maps; two surfaces
translating the same enum is how a product starts disagreeing with itself.

"What do you know about her" is answered the same way on all three surfaces,
from `channels.briefing`. It reads the state the engine has actually
accumulated — messages held, per-axis readings with their confidence, which
axes are moving — and it does not run a model to do it, because the answer is
already on disk. Uncertain readings are labelled rather than hidden: a number
presented without its confidence is exactly the confident-sounding claim this
project exists to refuse.

## Deliberately not built

`sexual-skills`, `kink` and the simulation engine (doc §32–38, §53). The
`intimacy` skill and its track are in; the narrower technique and kink
modules are not, because they depend on preference and boundary state being
trustworthy first, and that needs real usage to calibrate. Their guardrails
already exist and are tested: protected namespaces, consent typing and the
vault tier all refuse writes today.
