"""What the hot path is allowed to see, and how it is labelled.

The reply path reads the conversation from exactly one place: `AgentContext`.
While the ingest pipeline was the only thing that could put text there, a
request that carried the conversation was invisible to it. `/reply` parsed
`body.text` for its track and dropped the rest, `orchestrator.reply` had no
parameter to receive it, and `render_conversation()` answered
`CONVERSATION: (none)` -- so "we should stop talking" and "want to see a film
tomorrow" both produced the same generic small talk.

`FreshTurn` is the second way in, and the boundary around it is the point of
this file: it informs the reply, and it is **not evidence**. Invariant 1 says a
model-inferred assessment needs an evidence ref, and text handed over with a
request has none until it has been through ingest. Nothing here may become a
way to skip that.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sonder.agent.context import AgentContext, FreshTurn
from sonder.agent.orchestrator import handed_over, route_request
from sonder.storage.scope import Scope


def _ctx(messages=(), fresh=()) -> AgentContext:
    return AgentContext(
        scope=Scope(user_id=uuid4(), relationship_id=uuid4()),
        recent_messages=list(messages),
        fresh_turns=list(fresh),
    )


def _msg(sender: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(sender=sender, body=body)


def test_an_empty_context_still_says_so():
    assert _ctx().render_conversation() == "CONVERSATION: (none)"


def test_handed_over_text_is_rendered_rather_than_reported_as_none():
    rendered = _ctx(fresh=[FreshTurn(body="她说：在忙吗")]).render_conversation()
    assert "她说：在忙吗" in rendered
    assert "HANDED OVER WITH THIS REQUEST" in rendered
    assert "CONVERSATION: (none)" not in rendered


def test_the_stored_record_and_the_handed_over_text_stay_distinguishable():
    """A reader must be able to tell which turns were persisted and which were
    merely handed over, and the newest has to come last."""
    rendered = _ctx(
        messages=[_msg("target", "早")],
        fresh=[FreshTurn(body="她说：在忙吗")],
    ).render_conversation()

    assert "RECENT CONVERSATION:" in rendered
    assert "not yet in the record" in rendered
    assert rendered.index("早") < rendered.index("她说：在忙吗")


def test_an_unknown_speaker_is_not_printed_as_one():
    """A pasted block has no reliable speaker structure, so guessing one would
    put words in someone's mouth inside the prompt that decides what to say to
    them."""
    rendered = _ctx(fresh=[FreshTurn(body="在忙吗")]).render_conversation()
    assert "unknown:" not in rendered
    assert "在忙吗" in rendered


def test_a_named_speaker_is_printed():
    rendered = _ctx(
        fresh=[FreshTurn(body="在忙吗", speaker="target")]
    ).render_conversation()
    assert "target: 在忙吗" in rendered


def test_the_generator_and_critic_both_receive_it():
    """`render()` is the string the generator drafts from and the Critic scores
    `context_match` against. The fresh block has to survive into it, or the
    Critic judges a reply against a conversation it cannot see."""
    rendered = _ctx(fresh=[FreshTurn(body="她说：我们以后别联系了")]).render()
    assert "我们以后别联系了" in rendered


def test_the_intimate_prefix_never_reaches_a_model():
    request = route_request("/intimate 她说：在忙吗")
    assert request.text == "她说：在忙吗"
    assert [t.body for t in handed_over(request)] == ["她说：在忙吗"]


def test_whitespace_only_input_hands_over_nothing():
    assert handed_over(route_request("   ")) == []
