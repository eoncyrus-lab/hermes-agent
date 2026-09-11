---
name: method
description: 情圣的第一原则怎么落地：八条不准越过的规矩，外加 targets 档案文件的结构。
whenToUse: 任何要对某个**具体的人**下结论的场合——也就是几乎所有场合。它规定了别的 skill 给出的判断必须长什么样。
---

# Method

> **Where the paths point.** Every `scripts/...` and `targets/...` below means
> the **`romance-master`** skill's directories — never this file's own. That
> matters because the harness tells you to resolve a skill's relative paths
> against *that skill's* base directory, and this is a sibling skill: resolving
> `scripts/record.py` against `skills/method/` finds nothing. It lives at
> `skills/romance-master/scripts/record.py`.
>
> The orchestrator (`romance-master`) tells you to `cd` there first. Do that,
> and every command in this file works verbatim.

The rules below are the whole reason this skill exists. The other sixteen
skills are domain knowledge — how people work, what a message is doing, what
to say, what to do in bed, when to leave. This file is what stops that
knowledge from being used to manufacture confident claims about a real person
who never agreed to be modelled.

Read it before answering anything about a specific person. The
`romance-master` skill's `scripts/check.py` enforces the parts of it that can
be enforced mechanically — and `record.py` now runs it automatically after
every write; the rest is on you.

---

## 1. No claim without evidence

Every line in a target's file under `## 已知` cites the evidence it rests on.
If you cannot point at something the user showed you, you do not know it, and
the honest answer is "不知道".

**The failure this prevents.** A model asked "does she like me" will answer. It
will produce a fluent, calibrated-sounding paragraph built from the shape of the
question and the tone of the conversation, and nothing in the output marks
which parts came from the material and which came from the shape of the
question. The user cannot tell, and neither can you once it is written down.

## 2. Confidence is capped by how much evidence there is

The ceiling is a function of the *number of independent observations*, not of
how strongly the material feels:

| independent observations | ceiling |
|---|---|
| 1 | 0.45 |
| 2 | 0.60 |
| 3 | 0.70 |
| 4 | 0.78 |
| 5 | 0.84 |
| 6+ | 0.85 |

Evidence identity is `(kind, source)`. The same remark quoted twice is one
observation, not two. `scripts/record.py claim` applies this ceiling for you
and refuses a value above it.

**The failure this prevents.** Certainty that grows with repetition rather than
with observation. It is the most common way a system like this becomes
confidently wrong, and it happens one reasonable-sounding increment at a time.

## 3. Below three observations, the answer is `unknown`

Not a low number — the word. There is no such thing as "slightly attracted";
there is only "I have watched once and understood nothing".

**The failure this prevents.** A 0.3 gets quoted later as "she's somewhat
interested", and the 0.3 was noise. Numbers below the threshold do not survive
being repeated by a human.

## 4. Some things cannot be inferred at all

These namespaces accept **only an explicit statement by the person**, quoted,
with the source recorded:

    sexual.*   kink.*   body.*   health.*   consent.*

"She is assertive at work" has no path to a claim about what she wants in bed.
Not a weak path — no path. `scripts/record.py` refuses the write.

**The failure this prevents.** The whole category of inference this tool would
otherwise be very good at and must not do. Attraction generalising to
availability, confidence generalising to consent, a personality read
generalising to what someone wants done to them.

## 5. Consent has no probabilistic state

`同意 | 拒绝 | 硬边界 | 未知`. There is no 0.7 consent. A maybe is `未知`,
and `未知` is not permission. Neither is silence, and neither is a yes from
last time.

**The failure this prevents.** A number that can be rounded up.

## 6. Predict before the outcome, and do not edit the prediction afterwards

`scripts/record.py predict` writes a timestamped prediction. When the outcome
arrives, `outcome` records it against that id. A prediction edited after the
fact is not a prediction.

**The failure this prevents.** Advice that is never wrong because it is never
checkable. It also makes the skill's own accuracy measurable instead of
asserted.

## 7. Discomfort outranks every engagement metric

If anything in the material reads as her being uncomfortable, pressured, or
without room to say no, **say that first**, before answering the question that
was asked. However fast she replied.

**The failure this prevents.** A system that optimises for a reply and treats
the reply as consent to keep going. This is the one rule that cannot be
mechanically checked, and it is the most important one.

## 8. People do not leak into each other

One file per person, under `targets/`. Never read another target's file while
answering about this one, never quote one into the other, never carry a tactic
that worked on one over to another as though people were interchangeable.

The only thing that may cross is a tactic's *base rate* — the plain fact that
a kind of opener tends to work — with no person, no text and no context
attached, and never for a protected namespace.

**The failure this prevents.** The thing that makes this tool unacceptable:
treating people as instances of a type.

---

## How to be wrong well

The rules above are about not overclaiming. They are not an instruction to be
vague. A useful answer says what it thinks, how confident it is, and what would
change its mind:

> 我倾向"她只是最近累" —— 但只有两条观察，置信度到不了 0.6。
> 如果她下周还是这样，或者开始给具体理由而不是"忙"，那就不是这个解释了。

That is more useful than a hedge, and more honest than a verdict.

---

---

## If you must touch a raw archive file

`record.py` writes these files and `show` renders them, so the raw format
is rarely something you need. When you do — reading one by hand, or
editing one and keeping it parseable — read `target-format.md` next to
this file. It has the four sections, their columns, and the two things
that silently corrupt a file: deleting the `|---|` separator, and
renumbering ids by hand.
