#!/usr/bin/env python3
"""Check every target file against the rules in the `method` skill.

`record.py` refuses a bad write at the moment it happens. This catches what a
refusal cannot: files edited by hand, a claim whose evidence was later deleted,
a prediction whose outcome predates it, and the same words appearing under two
different people.

It is also the honest measure of what this form gives up. A database constraint
cannot be skipped; this can. Run it after recording anything, and treat a
non-zero exit as a stop rather than a warning.

    scripts/check.py            # every target
    scripts/check.py 小美        # one
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import AXES, CONSENT_STATES, PROTECTED, ROOT, TARGETS, ceiling_for, _read

# 规矩写在兄弟 skill `method` 里。这里指向真实路径，因为这段字符串会出现在
# 每一条报错里——指错地方比不指更糟。
METHOD_SKILL = ROOT.parent / "method" / "SKILL.md"

# 任何形如 ISO 日期的片段。全角/半角括号、有没有括号都能取到。
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def check_target(name: str) -> list[str]:
    """Every problem with one file, in the order a reader would hit them."""
    problems: list[str] = []
    meta, tables = _read(name)

    known = {row[0] for row in tables["证据"]}

    # 1. A claim cites evidence, and the evidence is there.
    for row in tables["已知"]:
        axis, value, cited = row[0], row[1], row[2]
        refs = {e.strip() for e in cited.split(",") if e.strip()}
        if not refs:
            problems.append(f"已知 {axis}：没有引用任何证据")
            continue
        missing = sorted(refs - known)
        if missing:
            problems.append(
                f"已知 {axis}：引用了 {'、'.join(missing)}，但这条证据已经不存在了"
            )

        # 2. Confidence is capped by the number of independent observations.
        try:
            number = float(value)
        except ValueError:
            problems.append(f"已知 {axis}：{value!r} 不是一个数")
            continue
        ceiling = ceiling_for(len(refs))
        if number > ceiling:
            problems.append(
                f"已知 {axis}：{number:.2f} 超过了 {len(refs)} 条观察的上限 "
                f"{ceiling:.2f}"
            )

        # 4. Protected namespaces cannot be inferred.
        if axis.startswith(PROTECTED):
            problems.append(
                f"已知 {axis}：受保护的命名空间，根本不允许推断"
            )
        if axis not in AXES:
            problems.append(f"已知 {axis}：不是已知的轴")

    # 5. Consent has no probabilistic state.
    for row in tables["边界"]:
        axis, state = row[0], row[1]
        if axis.startswith("consent.") and state not in CONSENT_STATES:
            problems.append(
                f"边界 {axis}：{state!r} 不是 {'、'.join(CONSENT_STATES)} 之一"
            )
        if not row[2].strip():
            problems.append(f"边界 {axis}：没有引述依据")

    # 6. A prediction is written before its outcome.
    seen: set[str] = set()
    for row in tables["预测"]:
        pid, written, _text, _conf, outcome = row[0], row[1], row[2], row[3], row[4]
        if pid in seen:
            problems.append(f"预测 {pid}：id 重复")
        seen.add(pid)
        if not written.strip():
            problems.append(f"预测 {pid}：没有时间戳，那它就不是一条预测")
        if outcome:
            # 用正则取日期，不按括号切。`record.py` 写的是全角「（）」，
            # 早先这里按 ASCII "(" 切，于是 when 变成整串、比较恒为假，
            # 这条检查就静默失效了——一条看着在执行、实际没执行的规矩。
            stamps = DATE_RE.findall(outcome)
            when = stamps[-1] if stamps else ""
            if when and written and when < written[:10]:
                problems.append(f"预测 {pid}：结果早于预测本身")

    # Housekeeping: a third party's record is meant to expire.
    retention = meta.get("retention_until", "")
    if retention:
        try:
            if date.fromisoformat(retention) < date.today():
                problems.append(
                    f"保留期已于 {retention} 到期——要么删掉，要么延期"
                )
        except ValueError:
            problems.append(f"retention_until {retention!r} 不是日期")

    return problems


def check_isolation(names: list[str]) -> list[str]:
    """The same words under two people, which is the leak this form cannot
    prevent -- only notice.

    Every duplicate is reported, with no minimum length. An earlier version
    skipped anything under twelve characters, which is a threshold that only
    makes sense in English: 「她说这周末想去看展」is nine characters and a
    complete sentence, and it crossed between two targets unnoticed.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    for name in names:
        _meta, tables = _read(name)
        for row in tables["证据"]:
            text = row[3].strip()
            if not text:
                continue
            if text in seen and seen[text] != name:
                problems.append(
                    f"同一条证据同时出现在 {seen[text]} 和 {name} 名下："
                    f"{text[:40]!r}"
                )
            seen[text] = name
    return problems


def main() -> int:
    if not TARGETS.exists():
        print("没有 targets/ 目录，无需检查")
        return 0

    names = (
        [sys.argv[1]] if len(sys.argv) > 1 else sorted(p.stem for p in TARGETS.glob("*.md"))
    )
    if not names:
        print("没有目标文件，无需检查")
        return 0

    total = 0
    for name in names:
        problems = check_target(name)
        if problems:
            total += len(problems)
            print(f"\n{name}:")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"{name}: ok")

    if len(names) > 1:
        leaks = check_isolation(names)
        if leaks:
            total += len(leaks)
            print("\n隔离：")
            for leak in leaks:
                print(f"  - {leak}")

    if total:
        print(f"\n{total} 处问题。这些是规矩，不是建议——见 "
              f"{METHOD_SKILL}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
