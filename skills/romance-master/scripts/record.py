#!/usr/bin/env python3
"""一个人一个文件，以及让它保持诚实的那些规矩。

这个脚本替代了一套 Postgres schema、六个数据库角色和行级安全——用大约
四百行加一个目录。它保住了真正要紧的部分：没有证据就不能有结论，置信度
不能超过证据条数撑得起的上限，有些命名空间根本不允许推断。

它放弃的东西值得点明：数据库约束不会被忘记，脚本会。`check.py` 只在被
运行的时候有用，而 agent 的自我约束只在 `SKILL.md` 被读进去的时候有用。
这个取舍是清醒做出的——见 README——也正是要盯着的地方。

用法：
    record.py new <名字> [--retention-days N]
    record.py list
    record.py show <名字>
    record.py evidence <名字> --source 原文 --text "她说……"
    record.py claim <名字> --axis attraction.romantic --value 0.7 --evidence e1,e2,e3
    record.py explicit <名字> --axis consent.kiss --state 同意 --quote "可以啊"
    record.py predict <名字> --text "她这周不会主动" --confidence 0.6
    record.py outcome <名字> --id p1 --result 应验
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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _targets_dir() -> Path:
    """档案放哪儿。

    默认在 skill 目录里，仓库自用时最省事。但档案是**关于真人的私人数据**，
    而 skill 目录是可以被复制、分享、被 git 管着的——所以用一个环境变量
    允许把它挪出去。装到 `~/.dsh/skills/` 之后就设 `ROMANCE_TARGETS`，
    私人数据就不再和技能代码混在一起。
    """
    override = os.environ.get("ROMANCE_TARGETS", "").strip()
    return Path(override).expanduser() if override else ROOT / "targets"


TARGETS = _targets_dir()

# 虚构内容单独放一个子目录，**不在 `targets/` 根下**。这一点是刻意的：
# `TARGETS.glob("*.md")` 是档案的枚举方式，把创作文件放在根下会被当成一份
# 档案读进来、被 check.py 按证据规则去验——而它根本不是证据。
#
# 防火墙（「虚构归虚构，档案归档案」）写进了四份 skill，但在此之前它只是
# 提示词层面的一句话。有了这个目录，它才是结构上的：创作材料有自己的家，
# 永远进不了 `证据` 表，也别想通过 check.py。
FICTION = TARGETS / "fiction"

# 置信度上限，按支撑一条结论的*独立*观察条数。规则写在
# `method` 那份 skill，这里放着是因为它是条程序能执行的规则。一条观察
# 撑不起 0.45 以上——没有任何对单句话的解读配得上更高。
CEILINGS = {0: 0.0, 1: 0.45, 2: 0.60, 3: 0.70, 4: 0.78, 5: 0.84}
CEILING_AT_SIX = 0.85

# 永远不可推断。只有当事人明确说出口、并且被引述的话才能写进这些命名空间，
# 而 `explicit` 是唯一会写的命令。
PROTECTED = ("sexual.", "kink.", "body.", "health.", "consent.")

# 同意是个枚举。没有 0.7 的同意，「未知」不是许可，「以前同意过」不是同意。
CONSENT_STATES = ("同意", "拒绝", "硬边界", "未知")

# 证据来源，按可信度从高到低。原文 > 截图 > 口述：转述会失真，而失真在
# 后面每一步都会被放大。
SOURCE_KINDS = ("原文", "截图", "口述")

# 预测的结局。没有「大概算应验」——那正是事后改口的形状。
OUTCOMES = ("应验", "部分应验", "没应验", "无法判断")

# 值得留的轴。不在这个列表上的结论，几乎总是模型在发明维度，而不是在观察。
AXES = (
    "attraction.emotional",
    "attraction.intellectual",
    "attraction.romantic",
    "attraction.sexual",
    "intimacy.conversational",
    "intimacy.emotional",
    "intimacy.physical",
    "intimacy.sexual",
    "bond.comfort",
    "bond.commitment",
    "bond.reciprocity",
    "bond.trust",
    "dynamics.initiative_balance",
    "dynamics.momentum",
)

SECTIONS = ("已知", "证据", "预测", "边界")

EMPTY = {
    "已知": "| 轴 | 值 | 证据 | 更新 |\n|---|---|---|---|",
    "证据": "| id | 日期 | 来源 | 内容 |\n|---|---|---|---|",
    "预测": "| id | 写于 | 预测 | 置信 | 结果 |\n|---|---|---|---|---|",
    "边界": "| 命名空间 | 状态 | 依据 |\n|---|---|---|",
}


class Refused(RuntimeError):
    """这次写入会破坏 `method` 那份 skill 里的某条规矩。"""


class AuditFailed(RuntimeError):
    """写入成功了，但写完之后的自检发现问题。

    和 `Refused` 分开，因为后果不同：`Refused` 是**什么都没发生**，这个
    **已经落盘了**，需要的是去修那个档案，不是重试。
    """


class Unwritable(RuntimeError):
    """档案根本写不进去——几乎总是沙箱，不是磁盘。

    没有这一层的话，权限拒绝会以一段 Python traceback 出现，而 agent 会把它
    读成「工具坏了」。真实原因是环境的：会话默认的 `workspace-write` 策略只
    允许改工作区内的文件，而档案默认在 skill 目录里——**只要会话的工作区不是
    这个仓库，每一次写入都会被拒。**
    """


# ------------------------------------------------------------------ 文件本身


def _display(path: Path) -> str:
    """尽量用相对路径显示，档案目录被挪到 skill 外面时退回绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def path_for(name: str) -> Path:
    """`targets/<名字>.md`，且不会跑到 `targets/` 外面去。

    名字是用户给的，最终会变成路径的一部分。没有这道检查，`../../.env`
    就是一个目标。
    """
    safe = name.strip()
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        raise Refused(f"目标名不合法：{name!r}")
    return TARGETS / f"{safe}.md"


def _read(name: str) -> tuple[dict[str, str], dict[str, list[list[str]]]]:
    path = path_for(name)
    if not path.exists():
        raise Refused(f"没有这个目标：{name}（先用 `record.py new {name}` 建）")

    meta: dict[str, str] = {}
    tables: dict[str, list[list[str]]] = {s: [] for s in SECTIONS}
    section = None
    # 数据从 `|---|---|` 分隔行之后才开始。没有这个标记，表头会被当成一条
    # 记录读回来、再连着一张新表头写出去，于是每写一次文件就长一张表头。
    reading_data: set[str] = set()

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if line.startswith("|") and section in tables:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                reading_data.add(section)
                continue
            if section in reading_data:
                tables[section].append(cells)
            continue
        if section is None and ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, tables


def _write(name: str, meta: dict[str, str], tables: dict[str, list[list[str]]]) -> None:
    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in meta.items()]
    lines += ["---", ""]
    for section in SECTIONS:
        lines += [f"## {section}", "", EMPTY[section]]
        for row in tables[section]:
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    path = path_for(name)
    body = "\n".join(lines).rstrip("\n") + "\n"
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise Unwritable(
            f"{path} 写不进去。\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            "这几乎总是**沙箱**，不是磁盘满、也不是文件权限配错。会话默认的\n"
            "`workspace-write` 策略只允许改工作区内的文件，而档案默认在 skill\n"
            "目录里——只要这个会话的工作区不包含下面这个目录，每一次写入都会\n"
            "被拒：\n\n"
            f"    {ROOT}\n\n"
            "三个办法，任选一个：\n"
            f"  1. 用上面这个目录、或者它的任何一个上级目录（比如仓库根）作为\n"
            "     这个会话的工作区\n"
            "  2. 把档案放到工作区里面：export ROMANCE_TARGETS=<工作区内的目录>\n"
            "  3. 这个会话改用 danger-full-access 权限\n\n"
            "改参数重试没有用——这不是这个脚本能解决的问题。"
        ) from exc
    _audit(name)


def _audit(name: str) -> None:
    """写完立刻自检，把审计从「自愿」变成「默认」。

    `check.py` 的效力完全取决于「到底跑了没有」—— 而在此之前，它靠 agent
    记得手动跑一次。那是这套东西最薄的一环：一条看着在执行、实际没人执行的
    规矩，比没有这条规矩更糟。挂在写入路径上之后，**通过写入发生的每一次
    修改都必然被验过一遍**，忘了跑这件事在结构上不可能发生。

    惰性导入是必须的：`check.py` 反过来 import 本模块，顶层导入会成环。
    异常不吞——自检自己崩掉是要看见的，静默失效正是要防的东西。
    """
    from check import check_target

    problems = check_target(name)
    if problems:
        raise AuditFailed(
            f"{name} 已经写进去了，但写完之后的自检发现问题：\n  - "
            + "\n  - ".join(problems)
            + "\n\n写入本身是成功的，不要去重试——去修上面这些，或者用 "
            "`forget` 把这份档案删掉重来。"
        )


def _next_id(rows: list[list[str]], prefix: str) -> str:
    highest = 0
    for row in rows:
        m = re.fullmatch(rf"{prefix}(\d+)", row[0] if row else "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1}"


def ceiling_for(count: int) -> float:
    return CEILINGS.get(count, CEILING_AT_SIX)


# -------------------------------------------------------------------- 各个命令


def cmd_new(args: argparse.Namespace) -> int:
    path = path_for(args.name)
    if path.exists():
        raise Refused(f"{args.name} 已经存在：{_display(path)}")
    TARGETS.mkdir(exist_ok=True)
    today = date.today()
    _write(
        args.name,
        {
            "name": args.name,
            "created": today.isoformat(),
            # 第三方的记录会过期。给一个不是自己用户的人建模，只有在上面
            # 写了一个截止日期的前提下才可以接受——所以没有别的建法。
            "retention_until": (today + timedelta(days=args.retention_days)).isoformat(),
        },
        {s: [] for s in SECTIONS},
    )
    print(f"已建立 {_display(path)}（保留 {args.retention_days} 天）")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    files = sorted(TARGETS.glob("*.md")) if TARGETS.exists() else []
    if not files:
        print("还没有任何目标。")
        return 0
    for f in files:
        meta, tables = _read(f.stem)
        print(
            f"{f.stem}\t证据 {len(tables['证据'])}\t已知 {len(tables['已知'])}\t"
            f"保留至 {meta.get('retention_until', '?')}"
        )
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if args.source not in SOURCE_KINDS:
        raise Refused(
            f"来源必须是 {'、'.join(SOURCE_KINDS)} 之一，收到 {args.source!r}。"
            "分不清来源，就分不清这条观察有多可信。"
        )
    rows = tables["证据"]
    # 身份是 (来源, 内容)。同一句话记两遍仍然是一条观察——这正是阻止
    # 「重复即确信」的东西。
    for row in rows:
        if row[2] == args.source and row[3] == args.text:
            print(row[0])
            return 0
    new_id = _next_id(rows, "e")
    rows.append([new_id, date.today().isoformat(), args.source, args.text])
    _write(args.name, meta, tables)
    print(new_id)
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    evidence = [e.strip() for e in args.evidence.split(",") if e.strip()]
    known_ids = {row[0] for row in tables["证据"]}
    missing = [e for e in evidence if e not in known_ids]
    if missing:
        raise Refused(
            f"这些证据 id 不存在：{'、'.join(missing)}。"
            "一条结论要引用它赖以成立的东西，而那东西得真的存在。"
        )
    if not evidence:
        raise Refused("一条结论至少要引用一条证据（--evidence e1[,e2]）")

    if args.axis.startswith(PROTECTED):
        raise Refused(
            f"「{args.axis}」是受保护的命名空间，证据再多也不能推断。"
            "如果当事人自己说过，用 `explicit` 记下来，并引述原话。"
        )
    if args.axis not in AXES:
        raise Refused(
            f"「{args.axis}」不在已知的轴里。关于列表之外的东西的结论，"
            "通常是在发明维度。\n已知："
            + "、".join(AXES)
        )

    ceiling = ceiling_for(len(set(evidence)))
    if args.value > ceiling:
        raise Refused(
            f"{args.value:.2f} 超过了 {len(set(evidence))} 条观察撑得起的上限"
            f"（上限 {ceiling:.2f}）。要么把值降下来，要么再拿证据来。"
        )

    rows = tables["已知"]
    rows = [r for r in rows if r[0] != args.axis]
    rows.append(
        [args.axis, f"{args.value:.2f}", ",".join(evidence), date.today().isoformat()]
    )
    rows.sort(key=lambda r: r[0])
    tables["已知"] = rows
    _write(args.name, meta, tables)
    print(f"{args.axis} = {args.value:.2f}（上限 {ceiling:.2f}）")
    return 0


def cmd_explicit(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if not args.axis.startswith(PROTECTED) and not args.axis.startswith("boundary."):
        raise Refused(f"「{args.axis}」不是受保护的命名空间，用 `claim` 记它")
    if not args.quote.strip():
        raise Refused("明确陈述必须带原话引述，否则它就是一个猜测")
    if args.axis.startswith("consent.") and args.state not in CONSENT_STATES:
        raise Refused(f"同意状态必须是 {'、'.join(CONSENT_STATES)} 之一")

    tables["边界"].append(
        [args.axis, args.state, f"{args.quote}（{date.today().isoformat()}）"]
    )
    _write(args.name, meta, tables)
    print(f"{args.axis} = {args.state}（明确陈述）")
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    # 预测也封顶。从一条观察出发对一个人的未来有 0.95 的把握，和从一条观察
    # 出发断言她的性格，是同一种错。这里用档案里的证据总数作代理——预测
    # 不引用具体证据，但它同样不该比手上的材料更自信。
    ceiling = ceiling_for(len(tables["证据"]))
    if args.confidence > ceiling:
        raise Refused(
            f"置信度 {args.confidence:.2f} 超过了手上 {len(tables['证据'])} 条"
            f"证据撑得起的上限（上限 {ceiling:.2f}）。"
        )
    new_id = _next_id(tables["预测"], "p")
    # 现在写下，带时间戳，之后不改——事后改过的预测不是预测。
    #
    # 时间戳必须用**本地**时间。这里原本写的是 `datetime.now(timezone.utc)`，
    # 而建档和记录结果都用 `date.today()`（本地）——两边混着比，在 UTC 以西
    # 的时区里会误报：洛杉矶晚上 20:00 写的预测，UTC 已经是次日，于是同一天
    # 20:05 记录的结果被判成「早于预测本身」。在 UTC 以东则表现为预测时间戳
    # 早于档案的建档日期，读起来像假的。
    tables["预测"].append(
        [
            new_id,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            args.text,
            f"{args.confidence:.2f}",
            "",
        ]
    )
    _write(args.name, meta, tables)
    print(new_id)
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    if args.result not in OUTCOMES:
        raise Refused(f"结果必须是 {'、'.join(OUTCOMES)} 之一，收到 {args.result!r}")
    for row in tables["预测"]:
        if row[0] == args.id:
            if row[4]:
                raise Refused(f"{args.id} 已经有结果了：{row[4]}")
            row[4] = f"{args.result}（{date.today().isoformat()}）"
            _write(args.name, meta, tables)
            print(f"{args.id}：{row[4]}")
            return 0
    raise Refused(f"没有这条预测：{args.id}")


def cmd_show(args: argparse.Namespace) -> int:
    meta, tables = _read(args.name)
    print(f"# {args.name}")
    print(f"保留至 {meta.get('retention_until', '?')}\n")

    # 空档案才说"什么都没有"。「边界」必须算进来：用户自己的档案（`我`）里
    # 只有边界——他的偏好是明确陈述，不需要证据——早先这里只看证据和已知，
    # 于是那份档案会显示成"关于这个人，我手上还什么都没有"，把仅有的内容
    # 全吞掉。
    if not any(tables[s] for s in SECTIONS):
        print("关于这个人，我手上还什么都没有。")
        return 0

    print(f"我手上的材料：{len(tables['证据'])} 条证据")
    if tables["已知"]:
        print("\n已知：")
        for axis, value, evidence, _when in tables["已知"]:
            mark = "（不确定）" if float(value) < 0.6 else ""
            print(f"  {axis} {value}{mark}  [证据 {evidence}]")
    else:
        print("\n还没有任何一条够得上成为结论。")

    if tables["边界"]:
        print("\n边界（只能来自明确陈述）：")
        for axis, state, basis in tables["边界"]:
            print(f"  {axis} = {state}  [{basis}]")

    pending = [r for r in tables["预测"] if not r[4]]
    if pending:
        print(f"\n还没结果的预测：{len(pending)} 条")
        for row in pending:
            print(f"  {row[0]}（{row[1]}，置信 {row[3]}）{row[2]}")
    done = [r for r in tables["预测"] if r[4]]
    if done:
        hit = sum(1 for r in done if r[4].startswith("应验"))
        print(f"\n已结算的预测：{len(done)} 条，其中应验 {hit} 条。")
    print("\n标了「不确定」的，就是证据还不够，别拿它下结论。")
    return 0


# ------------------------------------------------------------------ 虚构内容

FICTION_HEADER = (
    "# {name} · 创作\n\n"
    "> 这里是虚构内容——场景、文爱、幻想、角色扮演。\n"
    "> 它**永远不是证据**，永远不进 `## 证据` 表，永远不算关于她的信息。\n"
)


def _fiction_path(name: str) -> Path:
    # 复用同一条名字校验，别让 `../` 从这条路溜进来
    path_for(name)
    return FICTION / f"{name}.md"


def cmd_scene(args: argparse.Namespace) -> int:
    """存一段虚构内容。不碰档案，不进证据，不参与任何推断。"""
    if not path_for(args.name).exists():
        raise Refused(
            f"没有这个目标：{args.name}（先用 `record.py new {args.name}` 建）"
        )
    FICTION.mkdir(parents=True, exist_ok=True)
    path = _fiction_path(args.name)
    text = (
        path.read_text(encoding="utf-8")
        if path.exists()
        else FICTION_HEADER.format(name=args.name)
    )
    ids = re.findall(r"^## (s\d+) · ", text, re.M)
    new_id = f"s{max((int(i[1:]) for i in ids), default=0) + 1}"
    title = args.title.strip() or "（无题）"
    text = (
        text.rstrip("\n")
        + f"\n\n## {new_id} · {date.today().isoformat()} · {title}\n\n"
        + args.text.strip()
        + "\n"
    )
    path.write_text(text, encoding="utf-8")
    print(new_id)
    return 0


def cmd_scenes(args: argparse.Namespace) -> int:
    path = _fiction_path(args.name)
    rows = (
        re.findall(r"^## (s\d+) · (\S+) · (.*)$", path.read_text(encoding="utf-8"), re.M)
        if path.exists()
        else []
    )
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
        raise Refused(
            "这会永久删除这份档案和它的全部创作内容，不可恢复。"
            "确认要删就加 `--yes`。"
        )
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


def cmd_score(_args: argparse.Namespace) -> int:
    """跨档案汇总预测准确率。"""
    files = sorted(TARGETS.glob("*.md")) if TARGETS.exists() else []
    per: list[tuple[str, dict[str, int], int]] = []
    pending_all = 0
    for f in files:
        _meta, tables = _read(f.stem)
        rows = tables["预测"]
        if not rows:
            continue
        counts = {k: 0 for k in OUTCOMES}
        for row in rows:
            if not row[4]:
                continue
            for k in OUTCOMES:
                if row[4].startswith(k):
                    counts[k] += 1
                    break
        pending = sum(1 for r in rows if not r[4])
        pending_all += pending
        per.append((f.stem, counts, pending))

    if not per:
        print("还没有任何预测。")
        print()
        print("预测是这套东西唯一能变准的方式。先写几条：")
        print('  python3 scripts/record.py predict <名字> --text "..." --confidence 0.6')
        return 0

    settled = sum(sum(c.values()) for _n, c, _p in per)
    print("预测记分板（跨全部档案）\n")
    print(f"  {'':<16}{'已结算':>7}{'应验':>6}{'部分':>6}{'没应验':>8}{'待验证':>8}")
    for name, counts, pending in per:
        n = sum(counts.values())
        print(
            f"  {name:<16}{n:>7}{counts['应验']:>6}{counts['部分应验']:>6}"
            f"{counts['没应验']:>8}{pending:>8}"
        )

    if settled:
        hits = sum(c["应验"] for _n, c, _p in per)
        partials = sum(c["部分应验"] for _n, c, _p in per)
        misses = sum(c["没应验"] for _n, c, _p in per)
        rate = (hits + 0.5 * partials) / settled
        print(f"\n  合计结算 {settled} 条：应验 {hits}、部分 {partials}、没应验 {misses}")
        print(f"  命中率 {rate:.0%}（部分应验按半分算）")
        if settled < 10:
            print(f"\n  样本只有 {settled} 条，这个数字还不能说明什么。")
    if pending_all:
        print(f"  还有 {pending_all} 条没结果——等它们出结果再来看。")
    print(
        "\n判断错了的留在档案里，那是账，不是污点。"
        "这份记分板的意义是让你知道自己在哪类事上系统性偏乐观。"
    )
    return 0


# ---------------------------------------------------------------------- 参数


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="record.py", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="建立一个人的档案")
    new.add_argument("name")
    new.add_argument("--retention-days", type=int, default=365)
    new.set_defaults(func=cmd_new)

    sub.add_parser("list", help="正在记录谁").set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="已知什么，说人话")
    show.add_argument("name")
    show.set_defaults(func=cmd_show)

    ev = sub.add_parser("evidence", help="记下用户给你看的某件事")
    ev.add_argument("name")
    ev.add_argument("--source", required=True, help="、".join(SOURCE_KINDS))
    ev.add_argument("--text", required=True, help="它实际说的是什么")
    ev.set_defaults(func=cmd_evidence)

    claim = sub.add_parser("claim", help="记下一条结论，连同它的证据")
    claim.add_argument("name")
    claim.add_argument("--axis", required=True)
    claim.add_argument("--value", type=float, required=True)
    claim.add_argument("--evidence", required=True, help="e1,e2")
    claim.set_defaults(func=cmd_claim)

    exp = sub.add_parser("explicit", help="记下一句关于边界/禁区的原话")
    exp.add_argument("name")
    exp.add_argument("--axis", required=True)
    exp.add_argument("--state", required=True)
    exp.add_argument("--quote", required=True)
    exp.set_defaults(func=cmd_explicit)

    pred = sub.add_parser("predict", help="在结果出现之前冻结一条预测")
    pred.add_argument("name")
    pred.add_argument("--text", required=True)
    pred.add_argument("--confidence", type=float, required=True)
    pred.set_defaults(func=cmd_predict)

    out = sub.add_parser("outcome", help="记下实际发生了什么")
    out.add_argument("name")
    out.add_argument("--id", required=True)
    out.add_argument("--result", required=True, help="、".join(OUTCOMES))
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

    sub.add_parser("score", help="跨档案汇总预测准确率").set_defaults(func=cmd_score)
    return p


def main() -> int:
    args = build().parse_args()
    try:
        return args.func(args)
    except Refused as exc:
        # 有名有姓，且非零退出：一个调用方能读懂并照着办的拒绝，而不是一段
        # traceback，也不是一次静默的成功。
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2
    except AuditFailed as exc:
        # 2 是「什么都没发生」，1 是「发生了，但结果有问题」。这个区分重要：
        # 看到 2 应该改参数重试，看到 1 应该去修文件。
        print(f"自检未通过：{exc}", file=sys.stderr)
        return 1
    except Unwritable as exc:
        # 第三种：环境不允许写。同样什么都没发生，但改参数没用——要去改环境。
        print(exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
