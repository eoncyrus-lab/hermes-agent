"""Deterministic message features. No model, no interpretation.

Everything here is countable and reproducible. Interpretation lives in
signals.py, where it has to carry a confidence. Keeping the two apart means
beliefs can be recomputed without re-reading raw text -- and means a change
in model behaviour cannot silently rewrite history.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata

from ..domain.schemas.interaction import MessageFeatures, Speaker

EXTRACTOR_VERSION = "features/1.0.0"

_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]")
# CJK messages routinely stack clauses, so the question marker is rarely
# final: "明天有空吗？一起吃个饭" is a question followed by a proposal.
_QUESTION = re.compile(
    r"[?？]"
    r"|\b(what|why|how|when|where|who|which|are you|do you|did you|can you)\b"
    r"|(是不是|有没有|吗|呢|怎么|为什么|什么时候|哪里|干嘛|干什么|多少|谁在|好不好|行不行)"
)
_OPEN_ENDING = re.compile(r"(\.\.\.|…|~|,\s*$|、$)")
_DISCLOSURE = re.compile(
    r"\b(i feel|i felt|i'm scared|i was|honestly|to be honest)\b"
    r"|我(觉得|感觉|其实|担心|害怕|难过|开心)"
)
_INVITATION = re.compile(
    r"\b(want to|wanna|let's|shall we|are you free|meet up|grab)\b"
    r"|要不要|一起|有空吗|见面|约"
)
_PLAN = re.compile(
    r"\b(tomorrow|tonight|weekend|monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday|\d{1,2}(am|pm)|\d{1,2}:\d{2})\b"
    r"|明天|后天|周[一二三四五六日末]|下午|晚上|点见"
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def visible_length(text: str) -> int:
    """Count what a reader perceives, so CJK and Latin are comparable."""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
        if not ch.isspace()
    )


def extract(text: str, prior_texts: list[str] | None = None) -> MessageFeatures:
    low = text.lower().strip()
    emoji = _EMOJI.findall(text)
    references_prior = False
    if prior_texts:
        tokens = {t for t in re.findall(r"\w{3,}", low)}
        for p in prior_texts[-12:]:
            ptok = {t for t in re.findall(r"\w{3,}", p.lower())}
            if len(tokens & ptok) >= 2:
                references_prior = True
                break
    return MessageFeatures(
        char_len=visible_length(text),
        is_question=bool(_QUESTION.search(low)),
        ends_open=bool(_OPEN_ENDING.search(text.strip())),
        self_disclosure=bool(_DISCLOSURE.search(low)),
        references_prior=references_prior,
        contains_emoji=bool(emoji),
        emoji_count=len(emoji),
        contains_invitation=bool(_INVITATION.search(low)),
        contains_plan_commitment=bool(_PLAN.search(low)),
        topic_shift=False,
        extractor_version=EXTRACTOR_VERSION,
    )


class Baseline:
    """A person's own norms. The only thing a signal may be measured against.

    "She replied in four minutes" is not information. "She replied in four
    minutes when her median is fifty" is. Without this class every latency
    reading is an absolute, and absolutes are how the agent decides a polite
    person is interested.
    """

    def __init__(
        self,
        latencies: list[float],
        lengths: list[int],
        min_samples: int = 8,
    ) -> None:
        self.latencies = sorted(latencies)
        self.lengths = sorted(lengths)
        self.min_samples = min_samples

    @property
    def established(self) -> bool:
        return len(self.latencies) >= self.min_samples

    @property
    def median_latency(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None

    @property
    def median_length(self) -> float | None:
        return statistics.median(self.lengths) if self.lengths else None

    def latency_z(self, value: float) -> float | None:
        """Signed deviation in [-1, 1]. None when there is no baseline yet."""
        if not self.established or not self.latencies:
            return None
        med = statistics.median(self.latencies)
        if med <= 0:
            return None
        ratio = (med - value) / med
        return max(-1.0, min(1.0, ratio))

    def length_z(self, value: int) -> float | None:
        if len(self.lengths) < self.min_samples:
            return None
        med = statistics.median(self.lengths)
        if med <= 0:
            return None
        return max(-1.0, min(1.0, (value - med) / med))

    @classmethod
    def from_rows(cls, rows, speaker: Speaker) -> Baseline:
        want = speaker.value
        lat = [
            r.reply_latency_s
            for r in rows
            if r.sender == want and r.reply_latency_s is not None
        ]
        lens = [
            (r.features or {}).get("char_len", 0)
            for r in rows
            if r.sender == want and r.features
        ]
        return cls(lat, [x for x in lens if x])
