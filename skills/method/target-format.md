# The shape of a target file

> Loaded on demand. Read this only when you must read a raw archive file
> or hand-edit one — `record.py` writes them and `show` renders them, so
> you rarely need the raw format.

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
