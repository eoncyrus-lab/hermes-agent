#!/usr/bin/env python3
"""按 `method` 那份 skill 的规矩检查每一份档案。

`record.py` 在写入那一刻拒绝坏的写法。这个脚本抓拒绝抓不到的东西：手改过
的文件、证据后来被删掉的结论、早于预测本身的结果、到期没结算的预测和期限、
同一句话出现在两个人名下。

它也是这个形态放弃了什么的诚实量尺：数据库约束跳不过去，这个跳得过去。
`record.py` 每次写完会自动跑它的单份检查；跨档案的隔离检查只在手动跑时做。
退出码 1 只表示规矩被违反；到期、保留期这类待办照样打印，但不算失败。

    scripts/check.py            # 全部档案，外加隔离检查
    scripts/check.py 小美        # 一份
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import (  # noqa: E402
    AXES,
    DEADLINE_RESULTS,
    OUTCOMES,
    ROOT,
    SOURCE_KINDS,
    TARGETS,
    _read,
    ceiling_for,
    current_boundaries,
    independent_count,
    is_explicit_only,
    normalize,
    parse_ids,
    states_for,
)

# 规矩写在兄弟 skill `method` 里。这段路径会出现在每一条报错里——指错比不指更糟。
METHOD_SKILL = ROOT.parent / "method" / "SKILL.md"

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def check_target(name: str) -> list[str]:
    """一份档案违反规矩的地方，按读者会碰到的顺序。

    只放「文件本身不对」的问题。到期没结算、保留期到了这类「该去做一件事」的，
    在 `check_todos` 里——它们不是写坏了，不该让每一次无关的写入都报失败。
    """
    problems: list[str] = []
    meta, tables = _read(name)
    evidence = tables["证据"]
    known = {row["id"] for row in evidence}

    if meta.get("__stray__"):
        problems.append("文件里有不在任何表格里的行（手写的备注？）——挪进表格或删掉，否则写入会被拒绝")

    # 证据本身：来源合法，id 不重复。
    seen_ids: set[str] = set()
    for row in evidence + tables["不适"]:
        if row["id"] in seen_ids:
            problems.append(f"{row['id']}：id 重复")
        seen_ids.add(row["id"])
        if row["来源"] not in SOURCE_KINDS:
            problems.append(f"{row['id']}：来源 {row['来源']!r} 不是 {'、'.join(SOURCE_KINDS)} 之一")

    # 1. 结论引用证据，而且证据真的在。
    for row in tables["已知"]:
        axis = row["轴"]
        refs = parse_ids(row["证据"])
        if not refs:
            problems.append(f"已知 {axis}：没有引用任何证据")
            continue
        missing = sorted(set(refs) - known)
        if missing:
            problems.append(f"已知 {axis}：引用了 {'、'.join(missing)}，但这条证据已经不存在了")

        # 4. 受保护的命名空间根本不允许推断（任何一段命中都算）。
        if is_explicit_only(axis):
            problems.append(f"已知 {axis}：受保护的命名空间，根本不允许推断")
        elif axis not in AXES:
            problems.append(f"已知 {axis}：不是已知的轴")

        # 2. 置信度被独立观察条数封顶——按内容去重之后的条数。
        number = _as_float(row["值"])
        if number is None:
            problems.append(f"已知 {axis}：{row['值']!r} 不是一个数")
            continue
        n = independent_count(refs, evidence)
        ceiling = ceiling_for(n)
        if number > ceiling:
            problems.append(f"已知 {axis}：{number:.2f} 超过了 {n} 条独立观察的上限 {ceiling:.2f}")

    # 5. 明确陈述：状态是枚举，有引述依据。只查每个命名空间**当前**那一条——
    #    历史行是历史，旧版允许的自由文本状态不该让档案永远过不了检查。
    for row, _history in current_boundaries(tables["边界"]):
        axis, state = row["命名空间"], row["状态"]
        if not is_explicit_only(axis):
            problems.append(f"边界 {axis}：不是只接受明确陈述的命名空间，应该用 claim")
        allowed = states_for(axis)
        if state not in allowed:
            problems.append(f"边界 {axis}：{state!r} 不是 {'、'.join(allowed)} 之一")
        if not row["依据"].strip():
            problems.append(f"边界 {axis}：没有引述依据")
        if row["来源"] and row["来源"] not in SOURCE_KINDS:
            problems.append(f"边界 {axis}：来源 {row['来源']!r} 不合法")

    # 6. 预测：引用证据、不超上限、结果不早于预测、到期要结算。
    seen: set[str] = set()
    for row in tables["预测"]:
        pid, written, outcome = row["id"], row["写于"], row["结果"]
        if pid in seen:
            problems.append(f"预测 {pid}：id 重复")
        seen.add(pid)
        if not written.strip():
            problems.append(f"预测 {pid}：没有时间戳，那它就不是一条预测")
        refs = parse_ids(row["证据"])
        conf = _as_float(row["置信"])
        if conf is None:
            problems.append(f"预测 {pid}：置信 {row['置信']!r} 不是一个数")
        elif not refs:
            pass  # 旧格式预测没有证据列：结算过的是历史，没结算的在 check_todos 里提醒
        else:
            missing = sorted(set(refs) - known)
            if missing:
                problems.append(f"预测 {pid}：引用了不存在的证据 {'、'.join(missing)}")
            n = independent_count(refs, evidence)
            if conf > ceiling_for(n):
                problems.append(f"预测 {pid}：置信 {conf:.2f} 超过了 {n} 条独立观察的上限 {ceiling_for(n):.2f}")
        if outcome:
            if not outcome.startswith(OUTCOMES):
                problems.append(f"预测 {pid}：结果 {outcome!r} 不是 {'、'.join(OUTCOMES)} 之一")
            stamps = DATE_RE.findall(outcome)
            when = stamps[-1] if stamps else ""
            if when and written and when < written[:10]:
                problems.append(f"预测 {pid}：结果早于预测本身")
        if row["到期"] and not DATE_RE.fullmatch(row["到期"]):
            problems.append(f"预测 {pid}：到期日 {row['到期']!r} 不是日期")

    for row in tables["期限"]:
        if row["结果"] and not row["结果"].startswith(DEADLINE_RESULTS):
            problems.append(f"期限 {row['id']}：结果 {row['结果']!r} 不是 {'、'.join(DEADLINE_RESULTS)} 之一")
        if not row["结果"] and not DATE_RE.fullmatch(row["到期"]):
            problems.append(f"期限 {row['id']}：到期日 {row['到期']!r} 不是日期")

    retention = meta.get("retention_until", "")
    if not retention:
        problems.append("没有 retention_until——每份档案都要有截止日期（`extend` 能补上）")
    else:
        try:
            date.fromisoformat(retention)
        except ValueError:
            problems.append(f"retention_until {retention!r} 不是日期（`extend` 能改好）")

    return problems


def check_todos(name: str) -> list[str]:
    """该去做、但不算档案写坏了的事：到期结算、保留期、旧格式的遗留。"""
    todos: list[str] = []
    meta, tables = _read(name)
    today = date.today().isoformat()

    for row in tables["预测"]:
        if row["结果"]:
            continue
        if not parse_ids(row["证据"]):
            todos.append(f"预测 {row['id']}：旧格式，没有引用证据——结算掉（`outcome --id {row['id']}`）")
        elif row["到期"] and row["到期"] < today:
            todos.append(f"预测 {row['id']}：{row['到期']} 就到期了，还没结算（`outcome --id {row['id']}`）")

    # 用户自己的期限：到期要结算，否则这个期限会被无限延后。
    for row in tables["期限"]:
        if not row["结果"] and DATE_RE.fullmatch(row["到期"]) and row["到期"] < today:
            todos.append(f"期限 {row['id']}：{row['到期']} 到期了，去问用户结果（`outcome --id {row['id']}`）")

    # 第三方的记录会过期。
    retention = meta.get("retention_until", "")
    try:
        if retention and date.fromisoformat(retention) < date.today():
            todos.append(f"保留期已于 {retention} 到期——问用户：`extend` 延期，或者 `forget`")
    except ValueError:
        pass
    return todos


def check_isolation(names: list[str]) -> list[str]:
    """同一句话出现在两个人名下——这个形态防不住、只能发现的那种泄漏。

    不设最短长度：「她说这周末想去看展」九个字，是一句完整的话。
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    for name in names:
        _meta, tables = _read(name)
        for row in tables["证据"] + tables["不适"]:
            text = normalize(row["内容"])
            if not text:
                continue
            if text in seen and seen[text] != name:
                problems.append(f"同一条证据同时出现在 {seen[text]} 和 {name} 名下：{text[:40]!r}")
            seen.setdefault(text, name)
    return problems


def main() -> int:
    if not TARGETS.exists():
        print("没有 targets/ 目录，无需检查")
        return 0

    names = [sys.argv[1]] if len(sys.argv) > 1 else sorted(p.stem for p in TARGETS.glob("*.md"))
    if not names:
        print("没有目标文件，无需检查")
        return 0

    total = 0
    for name in names:
        problems = check_target(name)
        todos = check_todos(name)
        if problems:
            total += len(problems)
            print(f"\n{name}:")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"{name}: ok")
        for todo in todos:
            print(f"  待办：{todo}")

    if len(names) > 1:
        leaks = check_isolation(names)
        if leaks:
            total += len(leaks)
            print("\n隔离：")
            for leak in leaks:
                print(f"  - {leak}")

    if total:
        print(f"\n{total} 处问题。这些是规矩，不是建议——见 {METHOD_SKILL}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
