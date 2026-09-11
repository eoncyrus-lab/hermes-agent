#!/usr/bin/env python3
"""执行层的回归测试。

这套东西的全部主张是「规矩由程序执行，不由提示词劝阻」。那么执行层本身就不
能是靠人记得手测的——上一轮改 `show` 的时候我就真的踩坏过它（边界表被整个
吞掉），而当时没有任何东西会告诉我。

纯标准库，在临时目录里跑，不碰真实档案：

    python3 scripts/selftest.py
    python3 scripts/selftest.py -v      # 打印每一条

退出码 0 = 全过，1 = 有失败。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERBOSE = "-v" in sys.argv

_failures: list[str] = []
_count = 0


def _run(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HERE / script), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def rec(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run("record.py", args, env)


def chk(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run("check.py", args, env)


def case(label: str, ok: bool, detail: str = "") -> None:
    global _count
    _count += 1
    if ok:
        if VERBOSE:
            print(f"  ✓ {label}")
    else:
        _failures.append(label)
        print(f"  ✗ {label}")
        if detail:
            for line in detail.strip().splitlines():
                print(f"      {line}")


def refuses(label: str, args: list[str], env: dict[str, str]) -> None:
    """应当以退出码 2 拒绝（什么都没发生）。"""
    r = rec(args, env)
    case(label, r.returncode == 2, f"退出码 {r.returncode}，期望 2\n{r.stderr}")


def accepts(label: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    r = rec(args, env)
    case(label, r.returncode == 0, f"退出码 {r.returncode}\n{r.stderr}")
    return r


def target_file(env: dict[str, str], name: str) -> Path:
    return Path(env["ROMANCE_TARGETS"]) / f"{name}.md"


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="romance-selftest-"))
    env = dict(os.environ)
    env["ROMANCE_TARGETS"] = str(root)
    env.pop("PYTHONPATH", None)

    try:
        print("建立档案与写入")
        accepts("new", ["new", "小美"], env)
        accepts("evidence 原文", ["evidence", "小美", "--source", "原文", "--text", "她主动问我周末有没有空"], env)
        accepts("evidence 截图", ["evidence", "小美", "--source", "截图", "--text", "她记得我上周随口说的事"], env)
        accepts("claim", ["claim", "小美", "--axis", "attraction.romantic", "--value", "0.6", "--evidence", "e1,e2"], env)
        accepts("explicit", ["explicit", "小美", "--axis", "consent.kiss", "--state", "未知", "--quote", "再说吧"], env)
        accepts("predict", ["predict", "小美", "--text", "她这周不会主动", "--confidence", "0.6"], env)
        accepts("outcome", ["outcome", "小美", "--id", "p1", "--result", "应验"], env)
        accepts("show", ["show", "小美"], env)
        accepts("list", ["list"], env)

        print("拒绝路径（必须退出码 2，且什么都没写）")
        refuses("非法来源", ["evidence", "小美", "--source", "听说", "--text", "x"], env)
        refuses("置信度超上限", ["claim", "小美", "--axis", "attraction.romantic", "--value", "0.9", "--evidence", "e1"], env)
        refuses("推断禁区", ["claim", "小美", "--axis", "body.appearance", "--value", "0.3", "--evidence", "e1"], env)
        refuses("未知轴", ["claim", "小美", "--axis", "made.up", "--value", "0.3", "--evidence", "e1"], env)
        refuses("引用不存在的证据", ["claim", "小美", "--axis", "bond.trust", "--value", "0.3", "--evidence", "e9"], env)
        refuses("非法同意枚举", ["explicit", "小美", "--axis", "consent.kiss", "--state", "可能吧", "--quote", "x"], env)
        refuses("预测置信度超上限", ["predict", "小美", "--text", "x", "--confidence", "0.99"], env)
        refuses("非法结果枚举", ["outcome", "小美", "--id", "p1", "--result", "大概吧"], env)
        refuses("重复结算", ["outcome", "小美", "--id", "p1", "--result", "没应验"], env)
        refuses("不存在的目标", ["show", "查无此人"], env)

        print("拒绝之后档案没被改动")
        before = target_file(env, "小美").read_text(encoding="utf-8")
        rec(["claim", "小美", "--axis", "attraction.romantic", "--value", "0.99", "--evidence", "e1"], env)
        case("被拒绝的写入没有落盘", before == target_file(env, "小美").read_text(encoding="utf-8"))

        print("check.py")
        r = chk([], env)
        case("干净档案退出码 0", r.returncode == 0, r.stdout + r.stderr)

        print("写入路径上的自动自检")
        good = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(
            good.replace("| attraction.romantic | 0.60 | e1,e2 |", "| attraction.romantic | 0.99 | e1,e2 |"),
            encoding="utf-8",
        )
        r = rec(["evidence", "小美", "--source", "口述", "--text", "她说周末有空"], env)
        case("改坏之后写入报退出码 1", r.returncode == 1, f"退出码 {r.returncode}")
        case("自检的报错说明了问题在哪", "0.99" in r.stderr and "上限" in r.stderr, r.stderr)
        target_file(env, "小美").write_text(good, encoding="utf-8")
        r = rec(["evidence", "小美", "--source", "口述", "--text", "她说周末有空"], env)
        case("修好之后写入恢复正常", r.returncode == 0, r.stderr)

        print("check.py 抓得到手改")
        target_file(env, "小美").write_text(
            target_file(env, "小美").read_text(encoding="utf-8").replace(
                "| attraction.romantic | 0.60 | e1,e2 |", "| attraction.romantic | 0.99 | e1,e2 |"
            ),
            encoding="utf-8",
        )
        r = chk(["小美"], env)
        case("置信度拔高被抓到", r.returncode == 1 and "超过了" in r.stdout, r.stdout)

        # 报错里会指一份文件让人去读规矩。指向一个不存在的路径比不指更糟——
        # 之前它拼的是 `references/method.md`，而那个目录早就不在了。
        import re as _re

        pointed = _re.findall(r"(/\S*?\.md)", r.stdout + r.stderr)
        case("报错里给出了规矩所在文件的路径", bool(pointed), r.stdout)
        if pointed:
            case(
                "那个路径真实存在",
                all(Path(p).exists() for p in pointed),
                "、".join(p for p in pointed if not Path(p).exists()),
            )

        print("跨目标隔离")
        accepts("建第二个目标", ["new", "小丽"], env)
        accepts("记同样的证据", ["evidence", "小丽", "--source", "原文", "--text", "她主动问我周末有没有空"], env)
        r = chk([], env)
        case("同一句话出现在两个目标下被抓到", "隔离" in r.stdout, r.stdout)

        print("虚构内容与证据隔离")
        accepts("存一段虚构", ["scene", "小美", "--title", "雨夜", "--text", "她把伞收起来靠在门框上"], env)
        case(
            "虚构内容没有进档案",
            "她把伞收起来" not in target_file(env, "小美").read_text(encoding="utf-8"),
        )
        case(
            "虚构内容写在单独的子目录",
            (Path(env["ROMANCE_TARGETS"]) / "fiction" / "小美.md").exists(),
        )
        r = rec(["list"], env)
        case("list 不把 fiction 当成一份档案", "fiction" not in r.stdout, r.stdout)
        r = rec(["scenes", "小美"], env)
        case("scenes 列得出来", "雨夜" in r.stdout, r.stdout)
        r = chk([], env)
        case("check.py 不去验虚构内容", r.returncode in (0, 1), r.stdout)

        print("预测记分板")
        r = accepts("score", ["score"], env)
        case("记分板有结算数字", "命中率" in r.stdout and "应验" in r.stdout, r.stdout)

        print("forget")
        refuses("不给 --yes 不删", ["forget", "小丽"], env)
        case("拒绝之后档案还在", target_file(env, "小丽").exists())
        accepts("给 --yes 才删", ["forget", "小丽", "--yes"], env)
        case("档案被删掉", not target_file(env, "小丽").exists())
        case(
            "虚构内容一起删掉",
            not (Path(env["ROMANCE_TARGETS"]) / "fiction" / "小丽.md").exists(),
        )

        print("用户自己的档案（只有边界，没有观察）")
        accepts("建 我", ["new", "我", "--retention-days", "3650"], env)
        accepts("记偏好", ["explicit", "我", "--axis", "sexual.偏好", "--state", "明确", "--quote", "喜欢慢的"], env)
        r = rec(["show", "我"], env)
        case("show 不把只有边界的档案报成空的", "sexual.偏好" in r.stdout, r.stdout)

        print("空档案仍说实话")
        accepts("建一个空档案", ["new", "空档"], env)
        r = rec(["show", "空档"], env)
        case("真空档案报「什么都没有」", "什么都没有" in r.stdout, r.stdout)

        print("路径逃逸")
        refuses("目标名不能带斜杠", ["new", "../../etc/passwd"], env)
        refuses("目标名不能以点开头", ["new", ".hidden"], env)

        print("环境不允许写（沙箱 / 权限）")
        targets_dir = Path(env["ROMANCE_TARGETS"])
        targets_dir.chmod(0o500)
        r = rec(["new", "写不进"], env)
        targets_dir.chmod(0o700)
        case("不可写时报退出码 3", r.returncode == 3, f"退出码 {r.returncode}\n{r.stderr}")
        case("不是一段 traceback", "Traceback" not in r.stderr, r.stderr[-200:])
        case("说明里点出了工作区这个真实原因", "工作区" in r.stderr, r.stderr)
        case("说明里给了出口", "ROMANCE_TARGETS" in r.stderr, r.stderr)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if _failures:
        print(f"{len(_failures)}/{_count} 项失败：")
        for f in _failures:
            print(f"  - {f}")
        return 1

    print(f"全部 {_count} 项通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
