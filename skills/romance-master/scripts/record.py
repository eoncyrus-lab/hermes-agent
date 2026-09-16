#!/usr/bin/env python3
"""一个人一个文件，以及让它保持诚实的那些规矩。

这个脚本替代了一套 Postgres schema、六个数据库角色和行级安全——用几百行
加一个目录。它保住了真正要紧的部分：没有证据就不能有结论，置信度不能超过
独立观察条数撑得起的上限，有些命名空间根本不允许推断。

它放弃的东西值得点明：数据库约束不会被忘记，脚本会被绕开。这个取舍是清醒
做出的——见 README——也正是要盯着的地方。

用法：
    record.py new <名字> [--retention-days N]
    record.py extend <名字> --days N
    record.py list
    record.py show <名字>
    record.py evidence <名字> --source 原文 --text "她说……"
    record.py discomfort <名字> --source 原文 --text "她说有点不舒服"
    record.py claim <名字> --axis attraction.romantic --value 0.6 --evidence e1,e2
    record.py retract <名字> --axis attraction.romantic
    record.py explicit <名字> --axis consent.kiss --state 同意 --source 原文 --quote "可以啊"
    record.py predict <名字> --text "她这周不会主动" --confidence 0.45 --evidence e1 [--due 2026-10-01]
    record.py deadline <名字> --text "三个月内他不改，我就走" --due 2026-12-16
    record.py outcome <名字> --id p1 --result 应验
    record.py outcome <名字> --id d1 --result 做到了
    record.py scene <名字> --text "……" [--title 标题]
    record.py scenes <名字>
    record.py score
    record.py forget <名字> --yes

每次写入之后会自动跑一遍自检（`check.py` 的那套规则），发现问题就以退出码
1 报出来。这是刻意的：`check.py` 的价值完全取决于「跑了没有」，挂在写入路径
上之后，忘了跑在结构上不可能发生。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _targets_dir() -> Path:
    """档案放哪儿。

    默认在 skill 目录里，仓库自用时最省事。但档案是**关于真人的私人数据**，
    而 skill 目录是可以被复制、分享、被 git 管着的——所以用一个环境变量
    允许把它挪出去。
    """
    override = os.environ.get("ROMANCE_TARGETS", "").strip()
    return Path(override).expanduser() if override else ROOT / "targets"


TARGETS = _targets_dir()

# 虚构内容单独放一个子目录，**不在 `targets/` 根下**。`TARGETS.glob("*.md")`
# 是档案的枚举方式，创作文件放在根下会被当成一份档案读进来。有了这个目录，
# 「虚构归虚构，档案归档案」才是结构上的，而不只是提示词里的一句话。
FICTION = TARGETS / "fiction"

# 置信度上限，按支撑一条结论的**独立**观察条数（内容不同的证据条数）。
# 规则写在 `method`，这里是它能被程序执行的那一半。
CEILINGS = {0: 0.0, 1: 0.45, 2: 0.60, 3: 0.70, 4: 0.78, 5: 0.84}
CEILING_AT_SIX = 0.85

# 少于这么多条独立观察，档案里可以记数，但说出口时不报数（method 第 3 条）。
SPEAKABLE_AT = 3

# 永远不可推断。轴名里**任何一段**命中这些词都算——`attraction.sexual` 和
# `sexual.x` 是同一件事，只按前缀判断的话，换个前缀就绕过去了。
PROTECTED_SEGMENTS = ("sexual", "kink", "body", "health", "consent")

# 只接受明确陈述、但不属于上面那几类的命名空间：她亲口划的线。
EXPLICIT_ONLY_PREFIXES = ("boundary.",)

# 同意是个枚举。没有 0.7 的同意，「未知」不是许可，「以前同意过」不是同意。
CONSENT_STATES = ("同意", "拒绝", "硬边界", "未知")

# 其余明确陈述的状态。同样是枚举——「0.9 喜欢」是把概率偷运回来。
PREFERENCE_STATES = ("喜欢", "不喜欢", "想试", "不想", "硬边界", "明确", "未知")

# 证据来源，按可信度从高到低。原文 > 截图 > 口述。
SOURCE_KINDS = ("原文", "截图", "口述")

# 预测的结局。没有「大概算应验」——那正是事后改口的形状。
OUTCOMES = ("应验", "部分应验", "没应验", "无法判断")

# 用户给自己定的期限怎么结算。这不是预测，没有置信度，所以结局也不一样。
DEADLINE_RESULTS = ("做到了", "没做到", "改主意了")

# 值得留的轴。不在这个列表上的结论，几乎总是模型在发明维度。
# 没有任何带 sexual 的轴：关于她的性的一切只能来自 `explicit`。
AXES = (
    "attraction.emotional",
    "attraction.intellectual",
    "attraction.romantic",
    "intimacy.conversational",
    "intimacy.emotional",
    "intimacy.physical",
    "bond.comfort",
    "bond.commitment",
    "bond.reciprocity",
    "bond.trust",
    "dynamics.initiative_balance",
    "dynamics.momentum",
)

# 每张表的列。行在内存里是 {列名: 值} 的字典，读的时候**按表头对列**，
# 所以旧格式的文件（少了新加的列）照样能读，缺的列当空值。
SCHEMA = {
    "不适": ("id", "日期", "来源", "内容"),
    "已知": ("轴", "值", "证据", "更新"),
    "证据": ("id", "日期", "来源", "内容"),
    "预测": ("id", "写于", "预测", "置信", "证据", "到期", "结果"),
    "期限": ("id", "写于", "到期", "内容", "结果"),
    "边界": ("命名空间", "状态", "来源", "依据"),
}
# 写出的顺序。「不适」在最前面：第 7 条说它压过一切，文件里也就排第一。
SECTIONS = tuple(SCHEMA)

# 每张表里装自由文本的那一列。修复旧格式时，被切碎或折行的内容并回这里。
CONTENT_COLUMN = {"不适": "内容", "已知": "更新", "证据": "内容", "预测": "预测", "期限": "内容", "边界": "依据"}

Row = dict


class Refused(RuntimeError):
    """这次写入会破坏 `method` 那份 skill 里的某条规矩。"""


class AuditFailed(RuntimeError):
    """写入成功了，但写完之后的自检发现问题。**已经落盘了**，要修，不是重试。"""


class Unwritable(RuntimeError):
    """档案根本写不进去——几乎总是沙箱，不是磁盘。"""


class EncodingMismatch(RuntimeError):
    """渲染出来的文件读回来和内存里的不一样。什么都没写——这是脚本的 bug。"""


# ------------------------------------------------------------------ 单元格编码
#
# 档案是 markdown 表格，而用户贴进来的东西里会有 `|` 和换行——聊天记录几乎
# 总是多行的。不编码的话：换行之后的那几行不以 `|` 开头，读的时候被丢掉，
# 下一次写入就永久删掉了；`|` 会把一格切成两格，去重永远比不上，同一句话
# 可以记成六条「独立」观察。所以写之前编码，读之后解码，两边严格对称。
#
# 对称的前提是进来的文本先被规整：Windows 换行 `\r\n` 和孤立的 `\r` 一律变成
# `\n`，首尾空白去掉。文件只按 `\n` 分行——`str.splitlines()` 还会在 `\u2028`、
# `\x0c`、`\x85` 这些字符上断行，而它们在粘贴的文本里真的会出现。


def clean_text(text: str) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def encode_cell(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def decode_cell(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"\\": "\\", "|": "|", "n": "\n"}.get(nxt, ch + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_raw(line: str, escaped: bool) -> list[str]:
    """按 `|` 切一行，返回**不去空白**的原始片段（含行首 `|` 之前的那一段）。

    `escaped` 为真时 `\\|` 不算分隔符；旧格式文件不转义，反斜杠就是反斜杠。
    """
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if escaped and ch == "\\" and i + 1 < len(line):
            current.append(line[i : i + 2])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current))
    return cells


def split_row(line: str, escaped: bool = True) -> list[str]:
    """一行表格的单元格（未解码、去了首尾空白）。"""
    cells = split_raw(line.strip(), escaped)
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [c.strip() for c in cells]


def normalize(text: str) -> str:
    """证据的身份：去掉空白和标点差异之后的内容。「她说好。」和「她说好」是同一句。"""
    return "".join(
        ch for ch in text
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    ).lower()


def clean_axis(axis: str) -> str:
    """轴名规整成小写、无空白。`consent.Kiss` 和 `consent.kiss` 是同一件事。"""
    axis = axis.strip().lower()
    if not axis or any(ch.isspace() or ch in "|\\" for ch in axis) or "." not in axis:
        raise Refused(f"轴名不合法：{axis!r}（形如 attraction.romantic，不能带空白、| 或 \\）")
    return axis


# ------------------------------------------------------------------ 文件本身


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def path_for(name: str) -> Path:
    """`targets/<名字>.md`，且不会跑到 `targets/` 外面去。"""
    safe = name.strip()
    if (
        not safe
        or len(safe) > 64
        or safe.startswith(".")
        or any(ch in safe for ch in "/\\|:") or any(ch.isspace() and ch != " " for ch in safe)
    ):
        raise Refused(f"目标名不合法：{name!r}（不能带 / \\ | : 换行，不能以点开头）")
    return TARGETS / f"{safe}.md"


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Unwritable(_unwritable_message(path, exc)) from exc


def _unwritable_message(path: Path, exc: OSError) -> str:
    return (
        f"{path} 写不进去。\n"
        f"  {type(exc).__name__}: {exc}\n\n"
        "这几乎总是**沙箱**：会话的 `workspace-write` 策略只允许改工作区内的文件。\n"
        "三个办法，任选一个：\n"
        "  1. 用仓库根作为这个会话的工作区\n"
        "  2. export ROMANCE_TARGETS=<工作区内的目录>\n"
        "  3. 这个会话改用 danger-full-access 权限\n\n"
        "改参数重试没有用。"
    )


def _parse(text: str) -> tuple[dict[str, str], dict[str, list[Row]]]:
    """读一份档案。

    **旧格式**（这次改版之前写出的文件，认法是没有 `## 不适` 这一节）不转义：
    反斜杠照字面读；被 `|` 切碎的内容、被换行折断的内容在这里拼回去，下一次
    写入时就以新格式落盘。**新格式**里不在表格里的行（比如手写的备注）不去
    猜它属于哪一行——记在 `meta["__stray__"]`，`_write` 看到就拒绝写入，
    免得下一次写入把它悄悄抹掉。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    legacy = "\n## 不适" not in "\n" + text
    decode = (lambda s: s) if legacy else decode_cell
    meta: dict[str, str] = {}
    tables: dict[str, list[Row]] = {s: [] for s in SECTIONS}
    stray: list[str] = []
    section: str | None = None
    header: list[str] | None = None
    reading = False

    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            section = line[3:].strip()
            header, reading = None, False
            continue
        if section is None:
            if ":" in line and not line.startswith("#"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
            continue
        if section not in tables:
            if line.strip():
                stray.append(line)
            continue
        columns = header if header else list(SCHEMA[section])
        content = CONTENT_COLUMN[section]
        at = columns.index(content) if content in columns else len(columns) - 1
        if line.startswith("|"):
            if not reading:
                cells = split_row(line, escaped=not legacy)
                if cells and set("".join(cells)) <= set("-: "):
                    reading = True
                else:
                    header = cells
                continue
            pieces = split_raw(line.strip(), escaped=not legacy)[1:]  # 去掉行首 `|` 之前的空段
            if pieces and not pieces[-1].strip():
                pieces = pieces[:-1]
            extra = len(pieces) - len(columns)
            if extra > 0 and legacy:
                # 旧版内容里的 `|` 把一格切成了多格：原样拼回，保留原来的空白
                pieces = pieces[:at] + ["|".join(pieces[at : at + extra + 1])] + pieces[at + extra + 1 :]
            row = {col: "" for col in SCHEMA[section]}
            for col, value in zip(columns, pieces):
                if col in row:
                    row[col] = decode(value.strip())
            tables[section].append(row)
            continue
        if not line.strip():
            continue
        if legacy and reading and tables[section]:
            # 旧版内容里的换行：续行接回内容那一列。行尾是 `|` 的是这一行的最后一截，
            # 它后面跟着内容列之后的那几列；前面多出来的片段都属于内容。
            last = tables[section][-1]
            after = columns[at + 1 :]
            if line.endswith("|"):
                pieces = split_raw(line, escaped=False)[:-1]
                keep = len(pieces) - len(after)
                if keep < 1:
                    keep = 1
                body, tail = "|".join(pieces[:keep]), pieces[keep:]
            else:
                body, tail = line, []
            if content in last:
                last[content] = last[content] + "\n" + body.strip()
            for later, value in zip(after, tail):
                if later in last:
                    last[later] = value.strip()
            continue
        stray.append(line)

    if stray:
        meta["__stray__"] = "\n".join(stray)
    return meta, tables


def _read(name: str) -> tuple[dict[str, str], dict[str, list[Row]]]:
    path = path_for(name)
    if not path.exists():
        raise Refused(f"没有这个目标：{name}（先用 `record.py new {name}` 建）")
    return _parse(path.read_text(encoding="utf-8"))


def _render(meta: dict[str, str], tables: dict[str, list[Row]]) -> str:
    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in meta.items() if not k.startswith("__")]
    lines += ["---", ""]
    for section in SECTIONS:
        cols = SCHEMA[section]
        lines += [f"## {section}", ""]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for row in tables[section]:
            lines.append("| " + " | ".join(encode_cell(row.get(c, "")) for c in cols) + " |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _write(name: str, meta: dict[str, str], tables: dict[str, list[Row]]) -> None:
    path = path_for(name)
    if meta.get("__stray__"):
        raise Refused(
            f"{_display(path)} 里有不在任何表格里的行，这次写入会把它们抹掉，所以没有写：\n"
            + "\n".join(f"    {l}" for l in meta["__stray__"].splitlines()[:5])
            + "\n照 method/target-format.md 把它们挪进表格或删掉，再重试。"
        )
    for rows in tables.values():
        for row in rows:
            for key, value in row.items():
                row[key] = clean_text(value)
    body = _render(meta, tables)
    # 写之前读回来比一遍：编码只要有一处不对称，就是静默改写了用户的材料。
    if _parse(body)[1] != tables:
        raise EncodingMismatch(
            "档案编码读回来和写进去的不一致——这是脚本的 bug，**什么都没有写入**。"
            "把刚才那条命令原样报给维护者；材料先留在对话里，别丢。"
        )
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise Unwritable(_unwritable_message(path, exc)) from exc
    _audit(name)


def _audit(name: str) -> None:
    """写完立刻自检，把审计从「自愿」变成「默认」。

    违规（证据、上限、禁区、枚举）→ `AuditFailed`，退出码 1。
    待办（到期没结算、保留期到了）→ 只在 stderr 提醒，退出码不变——它们不是
    这次写入造成的，不该让每一次无关的写入都看起来失败。

    惰性导入是必须的：`check.py` 反过来 import 本模块。
    """
    from check import check_target, check_todos

    todos = check_todos(name)
    if todos:
        print(f"待办（{name}）：\n  - " + "\n  - ".join(todos), file=sys.stderr)
    problems = check_target(name)
    if problems:
        raise AuditFailed(
            f"{name} 已经写进去了，但写完之后的自检发现问题：\n  - "
            + "\n  - ".join(problems)
            + "\n\n写入本身是成功的，不要去重试——去修上面这些（结论错了用 `retract` 撤掉）。"
        )


def _next_id(rows: list[Row], prefix: str) -> str:
    highest = 0
    for row in rows:
        m = re.fullmatch(rf"{prefix}(\d+)", row.get("id", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1}"


def ceiling_for(count: int) -> float:
    return CEILINGS.get(count, CEILING_AT_SIX)


def is_protected(axis: str) -> bool:
    return any(seg in PROTECTED_SEGMENTS for seg in axis.lower().split("."))


def is_explicit_only(axis: str) -> bool:
    return is_protected(axis) or axis.startswith(EXPLICIT_ONLY_PREFIXES)


def states_for(axis: str) -> tuple[str, ...]:
    return CONSENT_STATES if "consent" in axis.lower().split(".") else PREFERENCE_STATES


def independent_count(cited: list[str], evidence_rows: list[Row]) -> int:
    """被引用的证据里，内容互不相同的有几条。

    按内容数而不是按 id 数：同一句话无论以什么途径被记了几次，都只是一次
    观察。手改文件造出来的重复 id 在这里也一样不算数。
    """
    by_id = {r["id"]: normalize(r["内容"]) for r in evidence_rows}
    return len({by_id[e] for e in set(cited) if e in by_id})


def parse_ids(raw: str) -> list[str]:
    return [e.strip() for e in raw.split(",") if e.strip()]


def _parse_due(raw: str) -> str:
    try:
        due = date.fromisoformat(raw.strip())
    except ValueError:
        raise Refused(f"到期日要写成 YYYY-MM-DD，收到 {raw!r}")
    if due < date.today():
        raise Refused(f"到期日 {due} 已经过去了——事后补写的期限不是期限")
    return due.isoformat()


def _warn_discomfort(tables: dict[str, list[Row]]) -> None:
    recent = [
        r for r in tables["不适"]
        if r["日期"] and r["日期"] >= (date.today() - timedelta(days=30)).isoformat()
    ]
    if recent:
        print(
            f"注意：这份档案最近 30 天有 {len(recent)} 条不适记录。"
            "回答任何关于推进的问题之前，先处理那个（method 第 7 条）。",
            file=sys.stderr,
        )


# -------------------------------------------------------------------- 各个命令


def cmd_new(args: argparse.Namespace) -> int:
    path = path_for(args.name)
    if path.exists():
        raise Refused(f"{args.name} 已经存在：{_display(path)}")
    if args.retention_days < 1:
        raise Refused("保留期至少 1 天")
    _ensure_dir(TARGETS)
    today = date.today()
    _write(
        args.name,
        {
            "name": args.name,
            "created": today.isoformat(),
            # 给一个不是用户自己的人建模，只有在上面写了截止日期的前提下
            # 才可以接受——所以没有别的建法。
            "retention_until": (today + timedelta(days=args.retention_days)).isoformat(),
        },
        {s: [] for s in SECTIONS},
    )
    print(f"已建立 {_display(path)}（保留 {args.retention_days} 天）")
    return 0


def cmd_extend(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if args.days < 1:
        raise Refused("延期至少 1 天")
    meta["retention_until"] = (date.today() + timedelta(days=args.days)).isoformat()
    _write(args.name, meta, tables)
    print(f"{args.name} 保留至 {meta['retention_until']}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    files = sorted(TARGETS.glob("*.md")) if TARGETS.exists() else []
    if not files:
        print("还没有任何目标。")
        return 0
    for f in files:
        meta, tables = _read(f.stem)
        extra = f"\t不适 {len(tables['不适'])}" if tables["不适"] else ""
        print(
            f"{f.stem}\t证据 {len(tables['证据'])}\t已知 {len(tables['已知'])}{extra}\t"
            f"保留至 {meta.get('retention_until', '?')}"
        )
    return 0


def _require_source(source: str) -> None:
    if source not in SOURCE_KINDS:
        raise Refused(
            f"来源必须是 {'、'.join(SOURCE_KINDS)} 之一，收到 {source!r}。"
            "分不清来源，就分不清这条观察有多可信。"
        )


def cmd_evidence(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    _require_source(args.source)
    if not args.text.strip():
        raise Refused("证据内容是空的")
    rows = tables["证据"]
    # 身份是内容，不是 (来源, 内容)。同一句话以原文、截图、口述各记一次，
    # 仍然是**一次**观察——否则换个来源就能把上限翻三倍。
    key = normalize(args.text)
    for row in rows:
        if normalize(row["内容"]) == key:
            old = row["来源"] if row["来源"] in SOURCE_KINDS else "口述"
            if SOURCE_KINDS.index(args.source) < SOURCE_KINDS.index(old):
                row["来源"] = args.source
                _write(args.name, meta, tables)
                print(f"{row['id']}（已有，来源升级为 {args.source}）")
            else:
                print(f"{row['id']}（已有，同一条观察不重复记）")
            return 0
    new_id = _next_id(rows, "e")
    rows.append({"id": new_id, "日期": date.today().isoformat(), "来源": args.source, "内容": args.text.strip()})
    _write(args.name, meta, tables)
    print(new_id)
    return 0


def cmd_discomfort(args: argparse.Namespace) -> int:
    """记下对方表达过的不适、退缩、勉强。method 第 7 条的落脚处。"""
    meta, tables = _read(args.name)
    _require_source(args.source)
    if not args.text.strip():
        raise Refused("内容是空的")
    rows = tables["不适"]
    key = normalize(args.text)
    for row in rows:
        if normalize(row["内容"]) == key:
            print(f"{row['id']}（已有）")
            return 0
    new_id = _next_id(rows, "u")
    rows.append({"id": new_id, "日期": date.today().isoformat(), "来源": args.source, "内容": args.text.strip()})
    _write(args.name, meta, tables)
    print(new_id)
    print("已记下。这一条会排在 `show` 的最前面——下一次回答先处理它。")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    evidence = parse_ids(args.evidence)
    if not evidence:
        raise Refused("一条结论至少要引用一条证据（--evidence e1[,e2]）")
    known_ids = {row["id"] for row in tables["证据"]}
    missing = [e for e in evidence if e not in known_ids]
    if missing:
        raise Refused(
            f"这些证据 id 不存在：{'、'.join(missing)}。"
            "一条结论要引用它赖以成立的东西，而那东西得真的存在。"
        )
    if is_explicit_only(args.axis):
        raise Refused(
            f"「{args.axis}」是受保护的命名空间，证据再多也不能推断。"
            "如果当事人自己说过，用 `explicit` 记下来，并引述原话。"
        )
    if args.axis not in AXES:
        raise Refused(
            f"「{args.axis}」不在已知的轴里。关于列表之外的东西的结论，"
            "通常是在发明维度。\n已知：" + "、".join(AXES)
        )
    if not 0 <= args.value <= 1:
        raise Refused("值在 0 到 1 之间")

    n = independent_count(evidence, tables["证据"])
    ceiling = ceiling_for(n)
    if args.value > ceiling:
        raise Refused(
            f"{args.value:.2f} 超过了 {n} 条独立观察撑得起的上限"
            f"（上限 {ceiling:.2f}）。去问用户要更多，而不是把数字压到刚好过线。"
        )

    rows = [r for r in tables["已知"] if r["轴"] != args.axis]
    rows.append({"轴": args.axis, "值": f"{args.value:.2f}", "证据": ",".join(evidence), "更新": date.today().isoformat()})
    rows.sort(key=lambda r: r["轴"])
    tables["已知"] = rows
    _write(args.name, meta, tables)
    print(f"{args.axis} = {args.value:.2f}（{n} 条独立观察，上限 {ceiling:.2f}）")
    if n < SPEAKABLE_AT:
        print(f"只有 {n} 条：说出口时是「还看不出来」，不报这个数。")
    _warn_discomfort(tables)
    return 0


def cmd_explicit(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if not is_explicit_only(args.axis):
        raise Refused(f"「{args.axis}」不是受保护的命名空间，用 `claim` 记它")
    if not args.quote.strip():
        raise Refused("明确陈述必须带原话引述，否则它就是一个猜测")
    _require_source(args.source)
    states = states_for(args.axis)
    if args.state not in states:
        raise Refused(f"「{args.axis}」的状态必须是 {'、'.join(states)} 之一，收到 {args.state!r}")

    # 她划过的硬边界，只有她自己的原话能解除——用户的转述不够。
    if args.state not in ("硬边界", "拒绝", "不想", "不喜欢", "未知"):
        limits = [r for r in tables["边界"] if r["命名空间"].lower() == args.axis and r["状态"] == "硬边界"]
        if limits and args.source == "口述":
            raise Refused(
                f"「{args.axis}」之前记过硬边界（{limits[-1]['依据']}）。解除一条硬边界要她的原文或截图，"
                "用户的转述不够。"
            )

    # 只追加，不覆盖：旧的陈述是历史，留着。`show` 按每个轴最后一条算「现在」。
    tables["边界"].append(
        {"命名空间": args.axis, "状态": args.state, "来源": args.source,
         "依据": f"{args.quote.strip()}（{date.today().isoformat()}）"}
    )
    _write(args.name, meta, tables)
    print(f"{args.axis} = {args.state}（明确陈述，{args.source}）")
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    """撤掉一条结论。证据留着——证据是发生过的事，结论只是你当时的读法。"""
    meta, tables = _read(args.name)
    before = len(tables["已知"])
    tables["已知"] = [r for r in tables["已知"] if r["轴"].lower() != args.axis]
    if len(tables["已知"]) == before:
        raise Refused(f"{args.name} 的已知里没有「{args.axis}」")
    _write(args.name, meta, tables)
    print(f"已撤掉 {args.axis}。它依据的证据还在。")
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if not args.text:
        raise Refused("预测内容是空的")
    evidence = parse_ids(args.evidence)
    known_ids = {row["id"] for row in tables["证据"]}
    missing = [e for e in evidence if e not in known_ids]
    if not evidence or missing:
        raise Refused(
            "预测要引用它依据的证据（--evidence e1[,e2]），而且那些证据得存在"
            + (f"；不存在：{'、'.join(missing)}" if missing else "")
            + "。用户给自己定的期限不是预测，用 `deadline`。"
        )
    if not 0 <= args.confidence <= 1:
        raise Refused("置信度在 0 到 1 之间")
    n = independent_count(evidence, tables["证据"])
    ceiling = ceiling_for(n)
    if args.confidence > ceiling:
        raise Refused(
            f"置信度 {args.confidence:.2f} 超过了 {n} 条独立观察撑得起的上限（上限 {ceiling:.2f}）。"
        )
    due = _parse_due(args.due) if args.due else ""
    new_id = _next_id(tables["预测"], "p")
    # 本地时间：建档和结算都用 `date.today()`，混用 UTC 会在跨日时误报。
    tables["预测"].append(
        {"id": new_id, "写于": datetime.now().strftime("%Y-%m-%d %H:%M"), "预测": args.text.strip(),
         "置信": f"{args.confidence:.2f}", "证据": ",".join(evidence), "到期": due, "结果": ""}
    )
    _write(args.name, meta, tables)
    print(new_id)
    _warn_discomfort(tables)
    return 0


def cmd_deadline(args: argparse.Namespace) -> int:
    """用户给自己定的期限。不是对她的预测，没有置信度，但同样先冻结。"""
    meta, tables = _read(args.name)
    if not args.text.strip():
        raise Refused("期限内容是空的")
    due = _parse_due(args.due)
    new_id = _next_id(tables["期限"], "d")
    tables["期限"].append(
        {"id": new_id, "写于": datetime.now().strftime("%Y-%m-%d %H:%M"), "到期": due,
         "内容": args.text.strip(), "结果": ""}
    )
    _write(args.name, meta, tables)
    print(f"{new_id}（{due} 到期；到期没结算，自检会报出来）")
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if args.id.startswith("d"):
        section, allowed = "期限", DEADLINE_RESULTS
    else:
        section, allowed = "预测", OUTCOMES
    if args.result not in allowed:
        raise Refused(f"{args.id} 的结果必须是 {'、'.join(allowed)} 之一，收到 {args.result!r}")
    for row in tables[section]:
        if row["id"] == args.id:
            if row["结果"]:
                raise Refused(f"{args.id} 已经有结果了：{row['结果']}")
            row["结果"] = f"{args.result}（{date.today().isoformat()}）"
            _write(args.name, meta, tables)
            print(f"{args.id}：{row['结果']}")
            return 0
    raise Refused(f"没有这条{section}：{args.id}")


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def current_boundaries(rows: list[Row]) -> list[tuple[Row, int]]:
    """每个命名空间最后一条陈述，以及它之前还有几条历史。"""
    latest: dict[str, Row] = {}
    counts: dict[str, int] = {}
    for row in rows:
        key = row["命名空间"].lower()
        latest[key] = row
        counts[key] = counts.get(key, 0) + 1
    return [(latest[k], counts[k] - 1) for k in latest]


def cmd_show(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    print(f"# {args.name}")
    if meta.get("__stray__"):
        print("⚠ 文件里有不在表格里的行——写入会被拒绝，先照 method/target-format.md 修好。")
    print(f"保留至 {meta.get('retention_until', '?')}\n")

    if not any(tables[s] for s in SECTIONS):
        print("关于这个人，我手上还什么都没有。")
        return 0

    if tables["不适"]:
        print("⚠ 不适（先处理这个，再回答别的——method 第 7 条）：")
        for r in reversed(tables["不适"]):
            print(f"  {r['id']}（{r['日期']}，{r['来源'] or '来源未记录'}）{r['内容']}")
        print()

    print(f"我手上的材料：{len(tables['证据'])} 条证据")
    if tables["已知"]:
        print("\n已知：")
        for r in tables["已知"]:
            n = independent_count(parse_ids(r["证据"]), tables["证据"])
            if _as_float(r["值"]) is None:
                print(f"  {r['轴']} {r['值']!r}（值坏了，跑 check.py）")
                continue
            if is_explicit_only(r["轴"]) or r["轴"] not in AXES:
                print(f"  {r['轴']} {r['值']}（这条不该是推断出来的结论——别用它，`retract` 掉）")
                continue
            mark = f"（只有 {n} 条独立观察——说出口是「还看不出来」，不报数）" if n < SPEAKABLE_AT else ""
            print(f"  {r['轴']} {r['值']}{mark}  [证据 {r['证据']}]")
    else:
        print("\n还没有任何一条够得上成为结论。")

    if tables["边界"]:
        print("\n边界（只能来自明确陈述；每个命名空间以最后一条为准）：")
        for r, history in current_boundaries(tables["边界"]):
            source = r["来源"] or "来源未记录"
            note = f"，之前还有 {history} 条" if history else ""
            print(f"  {r['命名空间']} = {r['状态']}  [{r['依据']}；{source}{note}]")
            # 以前说过的「不」不会因为后来一句话就从视野里消失
            past_no = [h for h in tables["边界"]
                       if h["命名空间"].lower() == r["命名空间"].lower() and h is not r
                       and h["状态"] in ("硬边界", "拒绝")]
            for h in past_no:
                print(f"    ↳ 之前她说过 {h['状态']}：{h['依据']}（{h['来源'] or '来源未记录'}）")
            if r["状态"] == "同意":
                print("    ↳ 这是当时对那件事的同意，不是现在的许可。每一次都要重新确认。")

    today = date.today().isoformat()
    pending = [r for r in tables["预测"] if not r["结果"]]
    if pending:
        print(f"\n还没结果的预测：{len(pending)} 条")
        for r in pending:
            due = f"，{r['到期']} 到期" if r["到期"] else ""
            late = "（已过期，去结算）" if r["到期"] and r["到期"] < today else ""
            print(f"  {r['id']}（{r['写于']}，置信 {r['置信']}{due}）{r['预测']}{late}")
    done = [r for r in tables["预测"] if r["结果"]]
    if done:
        hit = sum(1 for r in done if r["结果"].startswith("应验"))
        print(f"\n已结算的预测：{len(done)} 条，其中应验 {hit} 条。")

    open_deadlines = [r for r in tables["期限"] if not r["结果"]]
    if open_deadlines:
        print("\n用户给自己定的期限：")
        for r in open_deadlines:
            late = "（到期了，问他结果）" if r["到期"] < today else ""
            print(f"  {r['id']}（{r['到期']} 到期）{r['内容']}{late}")
    return 0


# ------------------------------------------------------------------ 虚构内容

FICTION_HEADER = (
    "# {name} · 创作\n\n"
    "> 这里是虚构内容——场景、文爱、幻想、角色扮演。\n"
    "> 它**永远不是证据**，永远不进 `## 证据` 表，永远不算关于她的信息。\n"
)


def _fiction_path(name: str) -> Path:
    path_for(name)  # 复用同一条名字校验
    return FICTION / f"{name.strip()}.md"


def cmd_scene(args: argparse.Namespace) -> int:
    """存一段虚构内容。不碰档案，不进证据，不参与任何推断。"""
    if not path_for(args.name).exists():
        raise Refused(f"没有这个目标：{args.name}（先用 `record.py new {args.name}` 建）")
    _ensure_dir(FICTION)
    path = _fiction_path(args.name)
    text = path.read_text(encoding="utf-8") if path.exists() else FICTION_HEADER.format(name=args.name)
    ids = re.findall(r"^## (s\d+) · ", text, re.M)
    new_id = f"s{max((int(i[1:]) for i in ids), default=0) + 1}"
    title = " ".join(args.title.split()) or "（无题）"
    # 正文里以 `## s数字 · ` 开头的行会被当成新的一段；缩进一格让它失效。
    body = re.sub(r"^(## s\d+ · )", r" \1", args.text.strip(), flags=re.M)
    text = text.rstrip("\n") + f"\n\n## {new_id} · {date.today().isoformat()} · {title}\n\n" + body + "\n"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise Unwritable(_unwritable_message(path, exc)) from exc
    print(new_id)
    return 0


def cmd_scenes(args: argparse.Namespace) -> int:
    path = _fiction_path(args.name)
    rows = re.findall(r"^## (s\d+) · (\S+) · (.*)$", path.read_text(encoding="utf-8"), re.M) if path.exists() else []
    if not rows:
        print(f"{args.name} 还没有创作内容。")
        return 0
    for sid, when, title in rows:
        print(f"  {sid}\t{when}\t{title}")
    print("\n这些是虚构内容，不是证据——别把它们当成关于她的信息。")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """永久删除一个人。档案和创作内容一起删。"""
    if not args.yes:
        raise Refused("这会永久删除这份档案和它的全部创作内容，不可恢复。确认要删就加 `--yes`。")
    removed = []
    for path in (path_for(args.name), _fiction_path(args.name)):
        if path.exists():
            path.unlink()
            removed.append(_display(path))
    if not removed:
        raise Refused(f"没有这个目标：{args.name}")
    for item in removed:
        print(f"已删除 {item}")
    print("档案已删除。这件事上，说到做到比留着方便更重要。")
    return 0


# ------------------------------------------------------------------ 记分板

OUTCOME_POINTS = {"应验": 1.0, "部分应验": 0.5, "没应验": 0.0}
BUCKETS = ((0.45, "≤0.45"), (0.60, "0.46–0.60"), (0.70, "0.61–0.70"), (1.00, ">0.70"))


def cmd_score(_args: argparse.Namespace) -> int:
    """跨档案的预测校准：说 0.6 的那些，是不是真的六成应验。"""
    files = sorted(TARGETS.glob("*.md")) if TARGETS.exists() else []
    scored: list[tuple[float, float]] = []
    unknown = pending = 0
    per: list[tuple[str, int, int, int]] = []
    deadlines = {k: 0 for k in DEADLINE_RESULTS}
    deadlines_open = 0
    for f in files:
        _meta, tables = _read(f.stem)
        n_scored = n_pending = 0
        for r in tables["预测"]:
            if not r["结果"]:
                pending += 1
                n_pending += 1
                continue
            result = next((k for k in OUTCOMES if r["结果"].startswith(k)), "")
            conf = _as_float(r["置信"])
            if result == "无法判断":
                unknown += 1
            elif result in OUTCOME_POINTS and conf is not None:
                scored.append((conf, OUTCOME_POINTS[result]))
                n_scored += 1
        for r in tables["期限"]:
            result = next((k for k in DEADLINE_RESULTS if r["结果"].startswith(k)), "")
            if result:
                deadlines[result] += 1
            else:
                deadlines_open += 1
        if tables["预测"]:
            per.append((f.stem, n_scored, n_pending, len(tables["预测"])))

    if not per and not sum(deadlines.values()) and not deadlines_open:
        print("还没有任何预测。")
        print("\n预测是这套东西唯一能变准的方式。先写几条：")
        print('  python3 scripts/record.py predict <名字> --text "..." --confidence 0.45 --evidence e1')
        return 0

    print("预测记分板（跨全部档案）\n")
    for name, n_scored, n_pending, total in per:
        print(f"  {name:<16}共 {total} 条\t可计分 {n_scored}\t待验证 {n_pending}")

    if scored:
        brier = sum((c - o) ** 2 for c, o in scored) / len(scored)
        hits = sum(o for _c, o in scored) / len(scored)
        print(f"\n  可计分 {len(scored)} 条（「无法判断」{unknown} 条不计入）")
        print(f"  实际应验率 {hits:.0%}（部分应验按半条算）；Brier 分数 {brier:.3f}（越低越准，全猜 0.5 是 0.250）")
        print("\n  校准——说的把握 vs 实际：")
        lower = -1.0
        for upper, label in BUCKETS:
            group = [(c, o) for c, o in scored if lower < c <= upper]
            lower = upper
            if group:
                said = sum(c for c, _o in group) / len(group)
                got = sum(o for _c, o in group) / len(group)
                gap = got - said
                lean = "偏乐观" if gap < -0.1 else "偏保守" if gap > 0.1 else "大致准"
                print(f"    {label:<10} {len(group):>3} 条  说 {said:.0%}  实际 {got:.0%}  {lean}")
        if len(scored) < 10:
            print(f"\n  样本只有 {len(scored)} 条，这些数字还不能说明什么。")
    elif unknown:
        print(f"\n  结算了 {unknown} 条，全是「无法判断」——没有能计分的。")
    if pending:
        print(f"  还有 {pending} 条没结果。")

    if sum(deadlines.values()) or deadlines_open:
        done = "、".join(f"{k} {v}" for k, v in deadlines.items())
        print(f"\n用户自己的期限：{done}；未结算 {deadlines_open}")

    print("\n判断错了的留在档案里，那是账，不是污点。看的是自己在哪一档上系统性偏乐观。")
    return 0


# ---------------------------------------------------------------------- 参数


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="record.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="建立一个人的档案")
    new.add_argument("name")
    new.add_argument("--retention-days", type=int, default=365)
    new.set_defaults(func=cmd_new)

    ext = sub.add_parser("extend", help="保留期延到今天起 N 天")
    ext.add_argument("name")
    ext.add_argument("--days", type=int, required=True)
    ext.set_defaults(func=cmd_extend)

    sub.add_parser("list", help="正在记录谁").set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="已知什么，说人话")
    show.add_argument("name")
    show.set_defaults(func=cmd_show)

    ev = sub.add_parser("evidence", help="记下用户给你看的某件事")
    ev.add_argument("name")
    ev.add_argument("--source", required=True, help="、".join(SOURCE_KINDS))
    ev.add_argument("--text", required=True, help="它实际说的是什么，多行原文照贴")
    ev.set_defaults(func=cmd_evidence)

    dis = sub.add_parser("discomfort", help="记下对方表达过的不适、退缩、勉强")
    dis.add_argument("name")
    dis.add_argument("--source", required=True, help="、".join(SOURCE_KINDS))
    dis.add_argument("--text", required=True)
    dis.set_defaults(func=cmd_discomfort)

    claim = sub.add_parser("claim", help="记下一条结论，连同它的证据")
    claim.add_argument("name")
    claim.add_argument("--axis", required=True)
    claim.add_argument("--value", type=float, required=True)
    claim.add_argument("--evidence", required=True, help="e1,e2")
    claim.set_defaults(func=cmd_claim)

    rt = sub.add_parser("retract", help="撤掉一条结论（证据保留）")
    rt.add_argument("name")
    rt.add_argument("--axis", required=True)
    rt.set_defaults(func=cmd_retract)

    exp = sub.add_parser("explicit", help="记下一句关于边界/禁区的原话")
    exp.add_argument("name")
    exp.add_argument("--axis", required=True)
    exp.add_argument("--state", required=True)
    exp.add_argument("--source", required=True, help="、".join(SOURCE_KINDS))
    exp.add_argument("--quote", required=True)
    exp.set_defaults(func=cmd_explicit)

    pred = sub.add_parser("predict", help="在结果出现之前冻结一条关于她的预测")
    pred.add_argument("name")
    pred.add_argument("--text", required=True)
    pred.add_argument("--confidence", type=float, required=True)
    pred.add_argument("--evidence", required=True, help="e1,e2")
    pred.add_argument("--due", default="", help="YYYY-MM-DD，可选")
    pred.set_defaults(func=cmd_predict)

    dl = sub.add_parser("deadline", help="冻结用户给自己定的期限")
    dl.add_argument("name")
    dl.add_argument("--text", required=True)
    dl.add_argument("--due", required=True, help="YYYY-MM-DD")
    dl.set_defaults(func=cmd_deadline)

    out = sub.add_parser("outcome", help="记下实际发生了什么（p 开头是预测，d 开头是期限）")
    out.add_argument("name")
    out.add_argument("--id", required=True)
    out.add_argument("--result", required=True,
                     help=f"预测：{'、'.join(OUTCOMES)}；期限：{'、'.join(DEADLINE_RESULTS)}")
    out.set_defaults(func=cmd_outcome)

    scene = sub.add_parser("scene", help="存一段虚构内容（场景、文爱、幻想）")
    scene.add_argument("name")
    scene.add_argument("--text", required=True)
    scene.add_argument("--title", default="")
    scene.set_defaults(func=cmd_scene)

    scenes = sub.add_parser("scenes", help="列出某个人名下的虚构内容")
    scenes.add_argument("name")
    scenes.set_defaults(func=cmd_scenes)

    forget = sub.add_parser("forget", help="永久删除一个人和他的全部内容")
    forget.add_argument("name")
    forget.add_argument("--yes", action="store_true", help="确认删除，不可恢复")
    forget.set_defaults(func=cmd_forget)

    sub.add_parser("score", help="跨档案的预测校准").set_defaults(func=cmd_score)
    return p


def main() -> int:
    args = build().parse_args()
    if hasattr(args, "name"):
        args.name = args.name.strip()
    for field in ("text", "quote", "title", "state"):
        if hasattr(args, field):
            setattr(args, field, clean_text(getattr(args, field)))
    try:
        if hasattr(args, "axis"):
            args.axis = clean_axis(args.axis)
        return args.func(args)
    except Refused as exc:
        # 2 = 什么都没发生，改参数重试
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2
    except AuditFailed as exc:
        # 1 = 发生了，但结果有问题，去修文件
        print(f"自检未通过：{exc}", file=sys.stderr)
        return 1
    except Unwritable as exc:
        # 3 = 环境不允许写，改参数没用
        print(exc, file=sys.stderr)
        return 3
    except EncodingMismatch as exc:
        # 4 = 脚本自己的 bug，什么都没写
        print(exc, file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
