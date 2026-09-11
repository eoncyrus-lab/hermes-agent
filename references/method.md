# Method

The rules below are the whole reason this skill exists. Everything else in
`references/` is domain knowledge — how people work, what a message is doing,
what to say. This file is what stops that knowledge from being used to
manufacture confident claims about a real person who never agreed to be
modelled.

Read it before answering anything about a specific person. `scripts/check.py`
enforces the parts of it that can be enforced mechanically; the rest is on you.

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

## The shape of a target file

Everything above is enforced by `scripts/record.py` writing into one file per
person under `targets/`. You should almost never edit one by hand — the script
is the only thing that applies the rules, and `scripts/check.py` exists to
catch the times you do. This section is here so that when you must read a file
you know what you are looking at, and when you must hand-edit one you keep it
parseable.

```markdown
---
name: 小美
created: 2026-09-12
retention_until: 2027-09-12
---

## 已知

| 轴 | 值 | 证据 | 更新 |
|---|---|---|---|
| attraction.romantic | 0.45 | e1 | 2026-09-12 |

## 证据

| id | 日期 | 来源 | 内容 |
|---|---|---|---|
| e1 | 2026-09-12 | 原文 | 她主动问我周末有没有空 |

## 预测

| id | 写于 | 预测 | 置信 | 结果 |
|---|---|---|---|---|
| p1 | 2026-09-12 01:07 | 她这周不会主动联系 | 0.45 | 应验（2026-09-12） |

## 边界

| 命名空间 | 状态 | 依据 |
|---|---|---|
| consent.kiss | 未知 | 再说吧（2026-09-12） |
```

Four sections, and each is a different kind of statement:

- **已知** is what you have concluded. `值` is a confidence that must not exceed
  the ceiling for the number of ids in `证据` (rule 2). `轴` must be one of the
  fourteen in `scripts/record.py`. No protected namespace may appear here.
- **证据** is the raw material, and the only section that records something
  rather than concluding something. `来源` is `原文`, `截图` or `口述`, in that
  order of trust. Identity is `(来源, 内容)`: recording the same thing twice
  returns the same id and does **not** raise the ceiling.
- **预测** is frozen at the moment it is written, with a local timestamp.
  结果 stays empty until `record.py outcome` fills it, once, permanently.
- **边界** accepts only a quoted statement (`--quote`), and for `consent.*`
  only one of `同意 | 拒绝 | 硬边界 | 未知`.

Things a reader should notice:

- The `|---|---|` separator after each header is what tells the parser where
  data begins. Delete it and the header row is read as a record.
- `retention_until` is not decoration. A third party's record is meant to
  expire; `check.py` reports an expired one as a problem rather than letting
  it sit there.
- Ids are `e1`, `e2` … and `p1`, `p2` … Hand-written ids that collide with
  generated ones corrupt the citation graph, which is why renumbering by hand
  is worse than it looks.
- A file is one person. There is no field for "us", and no place to put a
  second name.
