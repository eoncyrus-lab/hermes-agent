"""What every channel needs, in one place.

Three surfaces offer the same product -- Telegram, WeChat, the browser page --
and the failure mode of three surfaces is three products. What lives here is
exactly the part where they had already started to differ: which relationship
a chat is talking about, and how a result is put into words.

The split is deliberate. **How a message is worded is a product decision and
lives here**; **how an outcome is recorded is a transport decision and stays
with the channel**, because Telegram can press a button and WeChat has to type
a digit. Everything else -- who this is about, what the sentence is, what is
behind "why" -- is the same answer on every surface or it is a bug.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import structlog

from .config import REPO_ROOT
from .storage.db import dyad_session, user_session
from .storage.scope import Scope, UserScope

log = structlog.get_logger(__name__)

SELECTION_FILE = REPO_ROOT / "storage" / "chat-selection.json"

# The objective enum, in the words the user reads. An unknown value falls back
# to the raw name with its underscores opened up, so adding an objective does
# not silently start printing an empty label.
OBJECTIVES = {
    "establish_rapport": "建立联系",
    "increase_engagement": "把互动拉起来",
    "deepen_disclosure": "让她愿意多说一点",
    "build_tension": "加点暧昧",
    "move_offline": "约出来",
    "clarify_intent": "把意思问清楚",
    "de_escalate": "降温",
    "repair": "修复",
}

# The state axes, in the words the user reads. Same fallback rule.
AXES = {
    "attraction.emotional": "情感吸引",
    "attraction.intellectual": "聊得来的吸引",
    "attraction.romantic": "浪漫吸引",
    "attraction.sexual": "性吸引",
    "intimacy.conversational": "聊得开",
    "intimacy.emotional": "情感深度",
    "intimacy.physical": "身体上的亲近",
    "intimacy.sexual": "性方面的亲密",
    "bond.comfort": "相处是否放松",
    "bond.commitment": "投入程度",
    "bond.reciprocity": "有没有来有回",
    "bond.trust": "信任",
    "dynamics.initiative_balance": "谁更主动",
    "dynamics.momentum": "势头",
    "health.emotional_safety": "她在这段里的安全感",
    "health.mutual_respect": "互相尊重",
    "health.autonomy_preserved": "她的自主空间",
    "health.pressure_exerted_by_self": "你给的压力",
}

_TITLES = {
    "lighter": "更轻",
    "warmer": "更暖",
    "more ambiguous": "更含蓄",
    "more direct": "更直接",
}


class Selection:
    """Which relationship a chat is talking about, remembered across restarts.

    A file rather than a table, because it is UI state: losing it costs the
    user one question, and it is keyed by a chat id that no table knows about.
    Restarts used to lose it, which meant asking again for something the user
    had already answered -- and what they were asked for was a UUID.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._entries: dict[str, tuple[UUID, str]] = {}
        self.load()

    @property
    def path(self) -> Path:
        """Resolved lazily, so a test can redirect it after import.

        A channel builds its store at import time. Capturing the path there
        meant a test writing through one landed in the repository's own
        `storage/` -- a polluted working tree, and a running bot that loads a
        stranger's chat ids on its next start.
        """
        return self._path or SELECTION_FILE

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        except OSError as exc:
            log.warning("selection_unreadable", error=str(exc))
            return
        for chat_id, pair in (raw or {}).items():
            try:
                self._entries[str(chat_id)] = (UUID(pair[0]), pair[1])
            except (ValueError, TypeError, IndexError):
                # Written by an older version, or hand-edited. Not worth
                # failing over: the next message asks again.
                continue

    def save(self) -> None:
        """Best effort. A lost selection costs one question."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        chat: [str(rid), name]
                        for chat, (rid, name) in self._entries.items()
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("selection_unwritable", error=str(exc))

    def get(self, chat_id: object) -> tuple[UUID, str] | None:
        return self._entries.get(str(chat_id))

    def set(self, chat_id: object, value: tuple[UUID, str]) -> None:
        self._entries[str(chat_id)] = value
        self.save()

    def clear(self, chat_id: object) -> None:
        self._entries.pop(str(chat_id), None)
        self.save()


async def relationships(identity) -> list[tuple[UUID, str]]:
    """Every relationship this account has, oldest name first.

    Under the user-scoped policy, so it can only ever see the caller's own
    rows and there is no `WHERE user_id` here that a bug could drop.
    """
    from sqlalchemy import select

    from .storage.models import PersonProfileRow, RelationshipRow

    scope = UserScope(user_id=identity.user_id, identity_claim=identity.claim)
    async with user_session(scope) as session:
        rows = (
            await session.execute(
                select(RelationshipRow.id, PersonProfileRow.display_name)
                .join(
                    PersonProfileRow,
                    PersonProfileRow.id == RelationshipRow.target_person_id,
                )
                .where(
                    RelationshipRow.user_id == identity.user_id,
                    PersonProfileRow.subject == "target",
                )
                .order_by(PersonProfileRow.display_name, RelationshipRow.id)
            )
        ).all()
    return [(rid, name) for rid, name in rows]


def numbered(rows: Iterable[tuple[UUID, str]]) -> str:
    """The list as the user sees it: a number to type and a name to recognise.

    The UUID is left out on purpose. It is what `/who` used to demand, and
    asking somebody to paste an identifier they have never seen is the
    difference between a tool and a chore.
    """
    return "\n".join(
        f"{index}. {name or '(未命名)'}" for index, (_rid, name) in enumerate(rows, 1)
    )


def find(rows: list[tuple[UUID, str]], wanted: str) -> list[tuple[UUID, str]]:
    """Rows matching a number from the list, a UUID, or part of a name."""
    wanted = wanted.strip()
    if not wanted:
        return []
    for index, (relationship_id, _name) in enumerate(rows, 1):
        if wanted == str(index) or wanted == str(relationship_id):
            return [(relationship_id, _name)]
    return [row for row in rows if wanted in (row[1] or "")]


def resolve(rows: list[tuple[UUID, str]], wanted: str) -> tuple[str, object]:
    """One selection decision, shared by every channel.

    Returns `(kind, payload)` where kind is one of `ok`, `none`, `ambiguous`
    or `nomatch`. The caller turns that into its own words, because the words
    differ per surface and the decision must not.
    """
    if not rows:
        return "none", None
    if not wanted.strip():
        return "nomatch", rows
    hits = find(rows, wanted)
    if len(hits) == 1:
        return "ok", hits[0]
    if len(hits) > 1:
        return "ambiguous", hits
    return "nomatch", rows


async def current(identity, chat_id: object, selection: Selection):
    """Who this chat is about, chosen without asking when there is no choice.

    Auto-selection is not a guess. With one relationship there is nothing to
    disambiguate, and asking anyway costs the user a message every time; with
    several, the question is real and gets asked.
    """
    rows = await relationships(identity)

    # Before the empty check, not after: with no relationships at all, every
    # remembered id is stale by definition, and returning early would leave
    # dead state behind for the next message to consult.
    remembered = selection.get(chat_id)
    if remembered is not None:
        if any(rid == remembered[0] for rid, _ in rows):
            return remembered
        selection.clear(chat_id)

    if not rows:
        return None
    if len(rows) == 1:
        selection.set(chat_id, rows[0])
        return rows[0]
    return None


def render_reply(result, hint: str) -> str:
    """The sentence, and one short line saying what you can do with it.

    This used to be everything at once: the candidate, the objective, the
    rationale, the milestone, the notes, the alternatives and the outcome
    hint. Measured on a real suggestion that was 307 characters, of which 15
    were the thing the user came for -- in a phone chat window, a wall to
    scroll past before you could copy the one line you wanted.

    The rest is not deleted, it is moved behind a question. Someone standing
    in a chat window wants the sentence; someone who wants to know why will
    ask, and asking is one word. `hint` is the channel's, because pressing a
    button and typing a digit are not the same instruction.
    """
    return f"{result.suggestions[0].text}\n\n{hint}"


def render_detail(result) -> str:
    """What the short reply deliberately left out."""
    objective = OBJECTIVES.get(
        result.plan.objective.value, result.plan.objective.value.replace("_", " ")
    )
    lines = [f"— 想达到：{objective}"]
    if result.plan.rationale:
        lines.append(f"— {result.plan.rationale}")
    if result.plan.next_milestone:
        lines.append(f"— 有进展的话：{result.plan.next_milestone}")
    for note in result.notes:
        lines.append(f"— {note}")
    if len(result.suggestions) > 1:
        lines.append("")
        lines.append("其他说法：")
        for alt in result.suggestions[1:]:
            tone = _TITLES.get(alt.register, alt.register)
            lines.append(f"  （{tone}）{alt.text}")
    return "\n".join(lines)


# Whole-message matches only, on every surface. A sentence that happens to
# contain the words is a question about her, not a request for this.
BRIEF_WORDS = frozenset(
    {
        "你知道什么",
        "你都知道什么",
        "你知道她什么",
        "你都知道她什么",
        "她的情况",
        "什么情况",
        "简报",
        "你记得什么",
    }
)


async def briefing(scope: Scope) -> str:
    """What the engine has actually accumulated, said plainly.

    This is the half of the product that was invisible. The evidence model,
    the per-axis confidence and the trends are the reason this is not a
    general chatbot -- and none of it reached the user, who saw a suggestion
    and a rationale and had no way to tell whether sonder knew anything at all.

    Uncertain readings are labelled rather than hidden, because a number
    presented without its confidence is exactly the confident-sounding claim
    this project exists to refuse.
    """
    from .agent import context as ctx_mod

    async with dyad_session(scope) as session:
        ctx = await ctx_mod.build(session, scope)

    messages = len(ctx.recent_messages)
    if messages == 0 and ctx.state_row is None:
        return (
            "关于她，我手上还什么都没有。\n\n"
            "把她说的话、或者你们的聊天记录发给我，我就能开始记了。"
        )

    lines = [f"我手上关于她的材料：{messages} 条消息"]
    if ctx.state_row is None:
        lines.append("关系状态还没算出来 —— 材料还太少。")
    else:
        lines.append(f"关系状态（{ctx.state_row.as_of:%m月%d日} 算的）：")
        for group in ("attraction", "intimacy", "bond", "dynamics"):
            payload = getattr(ctx.state_row, group, None) or {}
            for axis, reading in payload.items():
                if not isinstance(reading, dict):
                    continue
                value = reading.get("value")
                if value is None:
                    continue
                confidence = reading.get("confidence", 0)
                label = AXES.get(f"{group}.{axis}", axis)
                hedge = "（不确定）" if confidence < 0.6 else ""
                lines.append(f"  {label} {value:.2f}{hedge}")

    health = (ctx.state_row.health if ctx.state_row else None) or {}
    live = [
        AXES.get(f"health.{axis}", axis)
        for axis, reading in health.items()
        if isinstance(reading, dict) and reading.get("confidence", 0) >= 0.35
    ]
    if live:
        lines.append("")
        lines.append("这些是关系健康度，跟推进无关，但它们优先：")
        for label in live:
            lines.append(f"  {label}")

    if ctx.trends:
        lines.append("")
        lines.append("在变的：")
        for trend in ctx.trends[:4]:
            direction = {"rising": "在涨", "falling": "在跌", "flat": "没动"}.get(
                trend.direction.value, trend.direction.value
            )
            lines.append(f"  {AXES.get(trend.axis, trend.axis)} {direction}")

    lines.append("")
    lines.append("标了「不确定」的，就是证据还不够，别拿它下结论。")
    return "\n".join(lines)


# The objective enum, in the words the user reads. An unknown value falls back
# to the raw name with its underscores opened up, so adding an objective does
# not silently start printing an empty label.


# Said when the models come back with nothing usable -- a provider error, an
# exhausted budget, a refusal that no hop could serve. The alternative was what
# happened first: WeChat logged the exception and sent nothing, so the user
# waited in front of a bot that had already given up, and the web page returned
# a 500 they could do nothing with.
PROVIDER_FAILED = (
    "这次没能拿到回答 —— 模型返回了空内容。已经记进日志了。\n"
    "换个说法、或者把问题拆小一点，通常就能过。"
)
