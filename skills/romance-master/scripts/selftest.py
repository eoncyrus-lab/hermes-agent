#!/usr/bin/env python3
"""执行层和 skill 体系的回归测试。

这套东西的全部主张是「规矩由程序执行，不由提示词劝阻」。那么执行层本身就不
能靠人记得手测——上一轮改 `show` 的时候我就真的踩坏过它（边界表被整个吞
掉），而当时没有任何东西会告诉我。

纯标准库，在临时目录里跑，不碰真实档案：

    python3 scripts/selftest.py
    python3 scripts/selftest.py -v      # 打印每一条

退出码 0 = 全过，1 = 有失败。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERBOSE = "-v" in sys.argv

_failures: list[str] = []
_count = 0


def _run(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HERE / script), *args], capture_output=True, text=True, env=env
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


def refuses(label: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """应当以退出码 2 拒绝（什么都没发生）。"""
    r = rec(args, env)
    case(label, r.returncode == 2, f"退出码 {r.returncode}，期望 2\n{r.stdout}{r.stderr}")
    return r


def accepts(label: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    r = rec(args, env)
    case(label, r.returncode == 0, f"退出码 {r.returncode}\n{r.stderr}")
    return r


def target_file(env: dict[str, str], name: str) -> Path:
    return Path(env["ROMANCE_TARGETS"]) / f"{name}.md"


def read_tables(env: dict[str, str], name: str) -> dict:
    """用和 record.py 同一个解析器读回来——测的是往返，不是字符串长相。"""
    sys.path.insert(0, str(HERE))
    import record  # noqa: E402

    return record._parse(target_file(env, name).read_text(encoding="utf-8"))[1]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="romance-selftest-"))
    env = dict(os.environ)
    env["ROMANCE_TARGETS"] = str(root)
    env.pop("PYTHONPATH", None)
    soon = (date.today() + timedelta(days=7)).isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()

    try:
        print("建立档案与写入")
        accepts("new", ["new", "小美"], env)
        accepts("evidence 原文", ["evidence", "小美", "--source", "原文", "--text", "她主动问我周末有没有空"], env)
        accepts("evidence 截图", ["evidence", "小美", "--source", "截图", "--text", "她记得我上周随口说的事"], env)
        accepts("claim", ["claim", "小美", "--axis", "attraction.romantic", "--value", "0.6", "--evidence", "e1,e2"], env)
        accepts("explicit", ["explicit", "小美", "--axis", "consent.kiss", "--state", "未知", "--source", "原文", "--quote", "再说吧"], env)
        accepts("predict", ["predict", "小美", "--text", "她这周不会主动", "--confidence", "0.6", "--evidence", "e1,e2", "--due", soon], env)
        accepts("outcome", ["outcome", "小美", "--id", "p1", "--result", "应验"], env)
        accepts("show", ["show", "小美"], env)
        accepts("list", ["list"], env)

        print("拒绝路径（必须退出码 2，且什么都没写）")
        refuses("非法来源", ["evidence", "小美", "--source", "听说", "--text", "x"], env)
        refuses("置信度超上限", ["claim", "小美", "--axis", "attraction.romantic", "--value", "0.9", "--evidence", "e1"], env)
        for label, axis in (("推断禁区（前缀）", "body.appearance"),
                            ("推断禁区（换了前缀的 sexual）", "attraction.sexual"),
                            ("推断禁区（大小写）", "Attraction.SEXUAL"),
                            ("boundary 只能 explicit", "boundary.contact")):
            r = refuses(label, ["claim", "小美", "--axis", axis, "--value", "0.3", "--evidence", "e1"], env)
            case(label + "——理由是受保护，不是轴不认识", "受保护" in r.stderr, r.stderr)
        refuses("预测内容不能为空", ["predict", "小美", "--text", "  ", "--confidence", "0.3", "--evidence", "e1"], env)
        refuses("未知轴", ["claim", "小美", "--axis", "made.up", "--value", "0.3", "--evidence", "e1"], env)
        refuses("引用不存在的证据", ["claim", "小美", "--axis", "bond.trust", "--value", "0.3", "--evidence", "e9"], env)
        refuses("非法同意枚举", ["explicit", "小美", "--axis", "consent.kiss", "--state", "可能吧", "--source", "原文", "--quote", "x"], env)
        refuses("偏好状态不能是数", ["explicit", "小美", "--axis", "sexual.likes", "--state", "0.9", "--source", "口述", "--quote", "x"], env)
        refuses("explicit 必须带合法来源", ["explicit", "小美", "--axis", "consent.kiss", "--state", "未知", "--source", "猜的", "--quote", "x"], env)
        refuses("预测置信度超上限", ["predict", "小美", "--text", "x", "--confidence", "0.99", "--evidence", "e1"], env)
        refuses("预测必须引用证据", ["predict", "小美", "--text", "x", "--confidence", "0.3", "--evidence", ""], env)
        refuses("预测到期日不能在过去", ["predict", "小美", "--text", "x", "--confidence", "0.3", "--evidence", "e1", "--due", past], env)
        refuses("非法结果枚举", ["outcome", "小美", "--id", "p1", "--result", "大概吧"], env)
        refuses("重复结算", ["outcome", "小美", "--id", "p1", "--result", "没应验"], env)
        refuses("不存在的目标", ["show", "查无此人"], env)
        refuses("保留期不能小于 1 天", ["new", "负数", "--retention-days", "-5"], env)

        print("拒绝之后档案没被改动")
        before = target_file(env, "小美").read_text(encoding="utf-8")
        rec(["claim", "小美", "--axis", "attraction.romantic", "--value", "0.99", "--evidence", "e1"], env)
        rec(["claim", "小美", "--axis", "attraction.sexual", "--value", "0.3", "--evidence", "e1"], env)
        case("被拒绝的写入没有落盘", before == target_file(env, "小美").read_text(encoding="utf-8"))

        print("check.py")
        r = chk([], env)
        case("干净档案退出码 0", r.returncode == 0, r.stdout + r.stderr)

        print("单元格编码：多行与竖线")
        chat = "她：今天好累\n我：怎么了 | 真的\n她：不想说\\n（这是字面的反斜杠 n）"
        accepts("建编码测试档", ["new", "编码"], env)
        accepts("多行带竖线的原文", ["evidence", "编码", "--source", "原文", "--text", chat], env)
        accepts("再写一条触发重写", ["evidence", "编码", "--source", "口述", "--text", "第二条"], env)
        rows = read_tables(env, "编码")["证据"]
        case("多行内容完整保存", rows and rows[0]["内容"] == chat, repr(rows[:1]))
        case("文件里没有裸换行把行切断", all(
            line.startswith(("|", "#", "---")) or not line.strip() or ":" in line
            for line in target_file(env, "编码").read_text(encoding="utf-8").splitlines()
        ))
        for _ in range(3):
            rec(["evidence", "编码", "--source", "原文", "--text", "她说 | 好啊"], env)
        rows = read_tables(env, "编码")["证据"]
        case("带竖线的同一句话只记一次", sum(1 for r in rows if r["内容"] == "她说 | 好啊") == 1, repr(rows))
        accepts("引述带竖线", ["explicit", "编码", "--axis", "consent.kiss", "--state", "同意", "--source", "原文", "--quote", "好 | 可以"], env)
        r = accepts("带竖线的边界 show 不崩", ["show", "编码"], env)
        for label, raw, expect in (
            ("Windows 换行", "第一行\r\n第二行\r\n", "第一行\n第二行"),
            ("孤立的 \\r", "甲\r乙", "甲\n乙"),
            ("Unicode 行分隔符", "丙\u2028丁", "丙\u2028丁"),
            ("换页符和 \\x85", "戊\x0c己\x85庚", "戊\x0c己\x85庚"),
            ("结尾反斜杠", "路径是 C:\\", "路径是 C:\\"),
        ):
            r = rec(["evidence", "编码", "--source", "原文", "--text", raw], env)
            got = [row["内容"] for row in read_tables(env, "编码")["证据"]]
            case(f"{label}：写入成功", r.returncode == 0, r.stderr[-300:])
            case(f"{label}：原样读回", expect in got, repr(got[-2:]))
        accepts("轴名带尾部空白和大写", ["explicit", "编码", "--axis", "Consent.Hug\t", "--state", "同意", "--source", "原文", "--quote", "抱吧"], env)
        case("轴名被规整成小写", any(r["命名空间"] == "consent.hug" for r in read_tables(env, "编码")["边界"]))
        accepts("标点不同的同一句", ["evidence", "编码", "--source", "原文", "--text", "她说好。"], env)
        accepts("", ["evidence", "编码", "--source", "原文", "--text", "她说好"], env)
        case("标点差异不算两条观察",
             sum(1 for r in read_tables(env, "编码")["证据"] if r["内容"].startswith("她说好")) == 1)

        print("手写在表格外的行不会被下一次写入抹掉")
        good = target_file(env, "编码").read_text(encoding="utf-8")
        target_file(env, "编码").write_text(good.replace("## 预测", "这是我手写的备注\n\n## 预测"), encoding="utf-8")
        before = target_file(env, "编码").read_text(encoding="utf-8")
        r = refuses("有表外行时拒绝写入", ["evidence", "编码", "--source", "口述", "--text", "新的"], env)
        case("备注还在，文件没动", before == target_file(env, "编码").read_text(encoding="utf-8"))
        r = chk(["编码"], env)
        case("check 报出表外行", r.returncode == 1 and "表格里" in r.stdout, r.stdout)
        target_file(env, "编码").write_text(good, encoding="utf-8")

        print("硬边界只有她的原话能解除")
        accepts("记硬边界", ["explicit", "编码", "--axis", "consent.sex", "--state", "硬边界", "--source", "原文", "--quote", "不行"], env)
        refuses("用户转述的同意不能盖过硬边界", ["explicit", "编码", "--axis", "consent.sex", "--state", "同意", "--source", "口述", "--quote", "她说可以了"], env)
        accepts("她的原文可以", ["explicit", "编码", "--axis", "consent.sex", "--state", "同意", "--source", "原文", "--quote", "现在可以了"], env)
        r = rec(["show", "编码"], env)
        case("show 仍然显示之前的硬边界", "之前她说过 硬边界" in r.stdout, r.stdout)

        print("证据身份是内容，不是来源")
        accepts("同一句话换来源", ["evidence", "小美", "--source", "口述", "--text", "她主动问我周末有没有空"], env)
        rows = read_tables(env, "小美")["证据"]
        case("没有新增 id", len(rows) == 2, repr(rows))
        refuses(
            "重复引用同一个 id 只算一条",
            ["claim", "小美", "--axis", "bond.comfort", "--value", "0.6", "--evidence", "e1,e1"],
            env,
        )
        # 手改出来的「内容相同、id 不同」也只算一条观察
        good = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(good.replace(
            "| e2 | ", "| e3 | " + date.today().isoformat() + " | 口述 | 她主动问我周末有没有空 |\n| e2 | ", 1), encoding="utf-8")
        refuses("claim 按内容数封顶", ["claim", "小美", "--axis", "bond.comfort", "--value", "0.6", "--evidence", "e1,e3"], env)
        target_file(env, "小美").write_text(good, encoding="utf-8")
        accepts("来源升级", ["evidence", "小美", "--source", "原文", "--text", "她记得我上周随口说的事"], env)
        case("升级后来源是原文", read_tables(env, "小美")["证据"][1]["来源"] == "原文")

        print("同意：以最后一条为准，同意不是现在的许可")
        accepts("后来拒绝", ["explicit", "编码", "--axis", "consent.kiss", "--state", "拒绝", "--source", "原文", "--quote", "别"], env)
        r = rec(["show", "编码"], env)
        case("show 只把最后一条当现在", "consent.kiss = 拒绝" in r.stdout and "consent.kiss = 同意" not in r.stdout, r.stdout)
        case("show 说明还有历史", "之前还有 1 条" in r.stdout, r.stdout)
        accepts("再同意", ["explicit", "编码", "--axis", "consent.kiss", "--state", "同意", "--source", "原文", "--quote", "可以"], env)
        r = rec(["show", "编码"], env)
        case("同意带着「不是现在的许可」", "不是现在的许可" in r.stdout, r.stdout)

        print("少于三条不报数")
        r = rec(["show", "小美"], env)
        case("show 标出不足三条", "还看不出来" in r.stdout, r.stdout)

        print("不适")
        accepts("discomfort", ["discomfort", "小美", "--source", "原文", "--text", "她说今天先别了"], env)
        r = rec(["show", "小美"], env)
        case("不适排在 show 最前面", 0 <= r.stdout.find("不适") < r.stdout.find("我手上的材料"), r.stdout)
        r = accepts("有不适时 claim 仍可写", ["claim", "小美", "--axis", "bond.trust", "--value", "0.3", "--evidence", "e1"], env)
        case("但会提醒先处理不适", "第 7 条" in r.stderr, r.stderr)

        print("期限")
        refuses("期限必须有到期日", ["deadline", "小美", "--text", "三个月", "--due", "明年"], env)
        accepts("deadline", ["deadline", "小美", "--text", "三个月内他不改，我就走", "--due", soon], env)
        refuses("期限不能用预测的结果词", ["outcome", "小美", "--id", "d1", "--result", "应验"], env)
        accepts("期限结算", ["outcome", "小美", "--id", "d1", "--result", "做到了"], env)

        print("到期没结算会被抓到")
        accepts("再立一个期限", ["deadline", "小美", "--text", "再等一个月", "--due", soon], env)
        good = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(good.replace(f"| {soon} | 再等一个月 |", f"| {past} | 再等一个月 |"), encoding="utf-8")
        r = chk(["小美"], env)
        case("过期未结算的期限作为待办报出", "待办" in r.stdout and "到期了" in r.stdout, r.stdout)
        case("待办不算违规（check 退出码 0）", r.returncode == 0, r.stdout)
        r = rec(["evidence", "小美", "--source", "口述", "--text", "一条无关的新证据"], env)
        case("有待办时无关写入仍然退出码 0", r.returncode == 0, r.stderr)
        case("但写入时提醒了待办", "待办" in r.stderr, r.stderr)
        target_file(env, "小美").write_text(good, encoding="utf-8")

        print("到期没结算的预测")
        accepts("带到期日的预测", ["predict", "小美", "--text", "她下周会回", "--confidence", "0.3", "--evidence", "e1", "--due", soon], env)
        good = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(good.replace(f"| {soon} |  |", f"| {past} |  |"), encoding="utf-8")
        r = chk(["小美"], env)
        case("过期未结算的预测作为待办报出", r.returncode == 0 and "就到期了" in r.stdout, r.stdout)
        target_file(env, "小美").write_text(good, encoding="utf-8")

        print("预测按引用的证据封顶，不按档案里的证据总数")
        accepts("建证据很多的档", ["new", "多证据"], env)
        for i in range(6):
            rec(["evidence", "多证据", "--source", "口述", "--text", f"不相干的事 {i} 号"], env)
        refuses("只引用一条时 0.6 被拒绝", ["predict", "多证据", "--text", "x", "--confidence", "0.6", "--evidence", "e1"], env)
        accepts("引用两条时 0.6 可以", ["predict", "多证据", "--text", "x", "--confidence", "0.6", "--evidence", "e1,e2"], env)

        print("写入路径上的自动自检")
        good = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(
            good.replace("| attraction.romantic | 0.60 | e1,e2 |", "| attraction.romantic | 0.99 | e1,e2 |"), encoding="utf-8"
        )
        r = rec(["evidence", "小美", "--source", "口述", "--text", "她说周末有空"], env)
        case("改坏之后写入报退出码 1", r.returncode == 1, f"退出码 {r.returncode}")
        case("自检的报错说明了问题在哪", "0.99" in r.stderr and "上限" in r.stderr, r.stderr)
        target_file(env, "小美").write_text(good, encoding="utf-8")
        r = rec(["evidence", "小美", "--source", "口述", "--text", "她说周末有空"], env)
        case("修好之后写入恢复正常", r.returncode == 0, r.stderr)

        print("check.py 抓得到手改")
        text = target_file(env, "小美").read_text(encoding="utf-8")
        target_file(env, "小美").write_text(
            text.replace("| attraction.romantic | 0.60 | e1,e2 |", "| attraction.romantic | 0.99 | e1,e2 |"), encoding="utf-8"
        )
        r = chk(["小美"], env)
        case("置信度拔高被抓到", r.returncode == 1 and "超过了" in r.stdout, r.stdout)
        pointed = re.findall(r"(/\S*?\.md)", r.stdout + r.stderr)
        case("报错里给出了规矩所在文件的路径", bool(pointed), r.stdout)
        if pointed:
            case("那个路径真实存在", all(Path(p).exists() for p in pointed), "、".join(pointed))
        target_file(env, "小美").write_text(
            text.replace("| attraction.romantic | 0.60 | e1,e2 |", "| attraction.sexual | 0.30 | e1,e2 |"), encoding="utf-8"
        )
        r = chk(["小美"], env)
        case("手写进去的 attraction.sexual 被抓到", r.returncode == 1 and "受保护" in r.stdout, r.stdout)
        target_file(env, "小美").write_text(
            text.replace("| attraction.romantic | 0.60 | e1,e2 |", "| attraction.romantic | high | e1,e2 |"), encoding="utf-8"
        )
        r = rec(["show", "小美"], env)
        case("值被手改坏了 show 也不崩", r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-300:])
        target_file(env, "小美").write_text(text, encoding="utf-8")

        print("旧格式档案（不转义、少列）照样能读，并在下次写入时修好")
        target_file(env, "旧档").write_text(
            "---\nname: 旧档\ncreated: 2026-09-12\nretention_until: 2099-01-01\n---\n\n"
            "## 已知\n\n| 轴 | 值 | 证据 | 更新 |\n|---|---|---|---|\n| intimacy.physical | 0.45 | e1 | 2026-09-12 |\n\n"
            "## 证据\n\n| id | 日期 | 来源 | 内容 |\n|---|---|---|---|\n"
            "| e1 | 2026-09-12 | 口述 | 第一行\n第二行 |\n| e2 | 2026-09-12 | 原文 | 她说 | 好啊 |\n"
            "| e3 | 2026-09-12 | 口述 | 开头\n最后一行 | 后半句 |\n| e4 | 2026-09-12 | 口述 | 路径 C:\\new 和 a\\\\b |\n\n"
            "## 预测\n\n| id | 写于 | 预测 | 置信 | 结果 |\n|---|---|---|---|---|\n"
            "| p1 | 2026-09-12 01:00 | 她会主动\n真的 | 0.45 |  |\n\n"
            "## 边界\n\n| 命名空间 | 状态 | 依据 |\n|---|---|---|\n| consent.sex | 同意 | 可以（2026-09-12） |\n"
            "| sexual.节奏 | 喜欢慢的 | 慢点（2026-09-12） |\n",
            encoding="utf-8",
        )
        r = chk(["旧档"], env)
        case("旧格式里当前的边界状态不合法被报出", r.returncode == 1 and "喜欢慢的" in r.stdout, r.stdout)
        case("旧格式没证据的预测是待办不是违规", "旧格式" in r.stdout and "待办" in r.stdout, r.stdout)
        accepts("追加一条合法状态", ["explicit", "旧档", "--axis", "sexual.节奏", "--state", "喜欢", "--source", "原文", "--quote", "慢点"], env)
        r = chk(["旧档"], env)
        case("追加之后历史里的自由文本不再报错", r.returncode == 0, r.stdout)
        accepts("旧格式上写入", ["evidence", "旧档", "--source", "口述", "--text", "新的一条"], env)
        t = read_tables(env, "旧档")
        case("折行的旧内容救回来了", t["证据"][0]["内容"] == "第一行\n第二行", repr(t["证据"]))
        case("被竖线切碎的旧内容拼回来了", t["证据"][1]["内容"] == "她说 | 好啊", repr(t["证据"]))
        case("旧边界行补上了来源列", t["边界"] and t["边界"][0]["状态"] == "同意" and t["边界"][0]["来源"] == "", repr(t["边界"]))
        case("折行最后一截里的竖线后半句没丢", t["证据"][2]["内容"] == "开头\n最后一行 | 后半句", repr(t["证据"][2]))
        case("旧内容里的反斜杠照字面保留", t["证据"][3]["内容"] == "路径 C:\\new 和 a\\\\b", repr(t["证据"][3]))
        case("旧预测的折行和后面的列都对", t["预测"] and t["预测"][0]["预测"] == "她会主动\n真的" and t["预测"][0]["置信"] == "0.45", repr(t["预测"]))
        accepts("旧预测可以结算", ["outcome", "旧档", "--id", "p1", "--result", "没应验"], env)

        print("推断出来的受保护结论能被撤掉")
        text = target_file(env, "旧档").read_text(encoding="utf-8")
        target_file(env, "旧档").write_text(text.replace("| intimacy.physical |", "| attraction.sexual |"), encoding="utf-8")
        r = rec(["show", "旧档"], env)
        case("show 标出不该存在的结论", "retract" in r.stdout, r.stdout)
        r = rec(["retract", "旧档", "--axis", "attraction.sexual"], env)
        case("retract 之后退出码 0", r.returncode == 0, r.stderr)
        case("结论没了，证据还在", not read_tables(env, "旧档")["已知"] and read_tables(env, "旧档")["证据"])

        print("跨目标隔离")
        accepts("建第二个目标", ["new", "小丽"], env)
        accepts("记同样的证据", ["evidence", "小丽", "--source", "原文", "--text", "她主动问我周末有没有空"], env)
        r = chk([], env)
        case("同一句话出现在两个目标下被抓到", "隔离" in r.stdout, r.stdout)

        print("虚构内容与证据隔离")
        accepts("存一段虚构", ["scene", "小美", "--title", "雨夜", "--text", "她把伞收起来靠在门框上\n## s9 · 2026-01-01 · 伪造"], env)
        case("虚构内容没有进档案", "她把伞收起来" not in target_file(env, "小美").read_text(encoding="utf-8"))
        case("虚构内容写在单独的子目录", (Path(env["ROMANCE_TARGETS"]) / "fiction" / "小美.md").exists())
        r = rec(["list"], env)
        case("list 不把 fiction 当成一份档案", "fiction" not in r.stdout, r.stdout)
        r = rec(["scenes", "小美"], env)
        case("scenes 列得出来", "雨夜" in r.stdout, r.stdout)
        case("正文里伪造的段落标题不算一段", "伪造" not in r.stdout, r.stdout)

        print("预测记分板")
        accepts("结算一条无法判断", ["predict", "小美", "--text", "y", "--confidence", "0.3", "--evidence", "e1"], env)
        accepts("", ["outcome", "小美", "--id", "p2", "--result", "无法判断"], env)
        r = accepts("score", ["score"], env)
        case("记分板有校准和 Brier", "Brier" in r.stdout and "校准" in r.stdout, r.stdout)
        case("无法判断不进分母", "可计分 2 条（「无法判断」1 条不计入）" in r.stdout and "实际应验率 50%" in r.stdout, r.stdout)
        case("期限单独统计", "做到了 1" in r.stdout, r.stdout)

        print("保留期")
        refuses("延期至少 1 天", ["extend", "小美", "--days", "0"], env)
        accepts("extend", ["extend", "小美", "--days", "30"], env)
        case("延期写进了 frontmatter", f"retention_until: {(date.today() + timedelta(days=30)).isoformat()}"
             in target_file(env, "小美").read_text(encoding="utf-8"))

        print("forget")
        refuses("不给 --yes 不删", ["forget", "小丽"], env)
        case("拒绝之后档案还在", target_file(env, "小丽").exists())
        accepts("给 --yes 才删", ["forget", "小丽", "--yes"], env)
        case("档案被删掉", not target_file(env, "小丽").exists())
        case("虚构内容一起删掉", not (Path(env["ROMANCE_TARGETS"]) / "fiction" / "小丽.md").exists())

        print("用户自己的档案（只有边界，没有观察）")
        accepts("建 我", ["new", "我", "--retention-days", "3650"], env)
        accepts("记偏好", ["explicit", "我", "--axis", "sexual.偏好", "--state", "喜欢", "--source", "口述", "--quote", "喜欢慢的"], env)
        r = rec(["show", "我"], env)
        case("show 不把只有边界的档案报成空的", "sexual.偏好" in r.stdout, r.stdout)

        print("空档案仍说实话")
        accepts("建一个空档案", ["new", "空档"], env)
        r = rec(["show", "空档"], env)
        case("真空档案报「什么都没有」", "什么都没有" in r.stdout, r.stdout)

        print("路径逃逸与非法名字")
        refuses("目标名不能带斜杠", ["new", "../../etc/passwd"], env)
        refuses("目标名不能以点开头", ["new", ".hidden"], env)
        refuses("目标名不能带竖线", ["new", "a|b"], env)
        refuses("目标名不能带换行", ["new", "a\nb"], env)

        print("档案目录还不存在（多层）")
        nested = dict(env, ROMANCE_TARGETS=str(root / "深" / "两层"))
        r = rec(["new", "嵌套"], nested)
        case("多层不存在的目录会被建出来", r.returncode == 0, r.stderr[-300:])

        print("环境不允许写（沙箱 / 权限）")
        targets_dir = Path(env["ROMANCE_TARGETS"])
        targets_dir.chmod(0o500)
        r = rec(["new", "写不进"], env)
        r2 = rec(["scene", "小美", "--text", "x"], env)
        targets_dir.chmod(0o700)
        case("不可写时报退出码 3", r.returncode == 3, f"退出码 {r.returncode}\n{r.stderr}")
        case("不是一段 traceback", "Traceback" not in r.stderr + r2.stderr, (r.stderr + r2.stderr)[-300:])
        case("说明里点出了沙箱这个真实原因", "沙箱" in r.stderr, r.stderr)
        case("说明里给了出口", "ROMANCE_TARGETS" in r.stderr, r.stderr)

        integrity()

    finally:
        for p in root.rglob("*"):
            try:
                p.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)

    print()
    if _failures:
        print(f"{len(_failures)}/{_count} 项失败：")
        for f in _failures:
            print(f"  - {f}")
        return 1

    print(f"全部 {_count} 项通过。")
    return 0


# ── skill 体系自身的完整性 ────────────────────────────────────────────────
#
# 这些每一条我都手动查过，而且手动查抓到过漂移：编排表里留着已经不存在的
# skill，README 的份数没跟着改，正文里写死的「其余十二份」。手动查只在想起
# 来的时候发生——和 `check.py` 当初的处境一模一样。放进这里就每次必跑。


def _cn(n: int) -> str:
    d = "零一二三四五六七八九"
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n % 10] if n % 10 else "")
    return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")


def integrity() -> None:
    print("skill 体系自身的完整性")
    sys.path.insert(0, str(HERE))
    import record  # noqa: E402

    skills_dir = HERE.parent.parent
    skills_md = sorted(skills_dir.glob("*/SKILL.md"))
    orch_text = (skills_dir / "romance-master" / "SKILL.md").read_text(encoding="utf-8")
    on_disk = {p.parent.name for p in skills_md}
    readme = (skills_dir.parent / "README.md").read_text(encoding="utf-8")
    texts = {p.parent.name: p.read_text(encoding="utf-8") for p in skills_md}

    # 1–2. 编排表：表里点名的都存在；除了编排者自己和两份横切的，每一份都在表里。
    rows = [line for line in orch_text.splitlines() if line.startswith("| **") and line.count("|") >= 5]
    in_table: set[str] = set()
    bad_rows = []
    for row in rows:
        cells = row.split("|")
        entry = re.findall(r"`([a-z-]+)`", cells[3])
        if not entry:
            bad_rows.append(cells[1].strip())
        in_table |= set(entry) | set(re.findall(r"`([a-z-]+)`", cells[4]))
    case("编排表每行都有入口 skill", bool(rows) and not bad_rows, "、".join(bad_rows))
    case("编排表点名的 skill 都存在", in_table <= on_disk, "不存在：" + "、".join(sorted(in_table - on_disk)))
    orphans = on_disk - {"romance-master", "method", "reasoning"} - in_table
    case("没有孤儿 skill（每一份都被编排表路由到）", not orphans, "没被路由：" + "、".join(sorted(orphans)))
    case("横切的 method 和 reasoning 在编排者里被点名",
         "`method`" in orch_text and "`reasoning`" in orch_text)

    # 3. 任何文件里反引号点名的「像 skill 名的词」，要么是真的 skill，要么是已知的非 skill 词。
    #    上一版把点名的词和一张硬编码名单取交集——名单外的错名字被交集直接滤掉，永远报不出来。
    commands = set(record.build()._subparsers._group_actions[0].choices)
    known_terms = commands | set(record.PROTECTED_SEGMENTS) | {"cd", "skill", "selftest", "cordis", "skill-filesystem", "standard"}
    unknown: list[str] = []
    docs = dict(texts)
    docs["method/target-format.md"] = (skills_dir / "method" / "target-format.md").read_text(encoding="utf-8")
    docs["README.md"] = readme
    for where, text in docs.items():
        for tok in set(re.findall(r"`([a-z][a-z-]*)`", text)):
            if tok not in on_disk and tok not in known_terms:
                unknown.append(f"{where}->{tok}")
    case("反引号点名的名字都认得（skill 或 record.py 命令）", not unknown, "、".join(sorted(unknown)))

    # 4. 文档里写的 record.py 命令都真实存在。
    used = set()
    for text in docs.values():
        used |= set(re.findall(r"record\.py (\w+)", text))
    case("文档里用到的 record.py 子命令都存在", used <= commands, "不存在：" + "、".join(sorted(used - commands)))

    # 5. frontmatter 只有 harness 认的三个字段，name 与目录名一致。
    bad_fm, mismatched = [], []
    for name, text in texts.items():
        fm = text.split("---")[1]
        extra = [l.split(":")[0] for l in fm.strip().splitlines()
                 if l.strip() and not l.startswith(("name:", "description:", "whenToUse:"))]
        if extra:
            bad_fm.append(f"{name}({','.join(extra)})")
        if f"name: {name}" not in fm:
            mismatched.append(name)
    case("frontmatter 只有 name/description/whenToUse", not bad_fm, "、".join(bad_fm))
    case("name 与目录名一致", not mismatched, "、".join(mismatched))

    # 6. skill 里点名的 .md 文件真实存在。
    missing = [f"{n}->{ref}" for n, t in texts.items()
               for ref in set(re.findall(r"`([a-z-]+\.md)`", t)) if not list(skills_dir.glob(f"*/{ref}"))]
    case("skill 里引用的 .md 都存在", not missing, "、".join(missing))

    # 7. 这是一个 agent：除编排者外，每一份开头都指回编排者。
    unguarded = [n for n, t in texts.items()
                 if n != "romance-master" and "romance-master 体系" not in t.split("\n## ")[0]]
    case("每份 skill 开头都指回 romance-master", not unguarded, "缺：" + "、".join(sorted(unguarded)))

    # 8. 份数：README 里的总数是当前的；任何文件里「其余/另外 N 份」都和实际一致。
    forms = [f"{len(on_disk)} 份 skill", f"{len(on_disk)}份 skill",
             f"{_cn(len(on_disk))}份 skill", f"{_cn(len(on_disk))} 份 skill"]
    case("README 里的 skill 份数是当前的", any(f in readme for f in forms),
         f"实际 {len(on_disk)} 份，README 里没找到 {' / '.join(forms[:3])}")
    stale = []
    others = _cn(len(on_disk) - 1)
    for where, text in docs.items():
        for word in re.findall(r"(?:其余|另外)([零一二三四五六七八九十]+|\d+) ?份", text):
            if word not in (others, str(len(on_disk) - 1)):
                stale.append(f"{where}：其余{word}份")
        stale += [f"{where}：{m}" for m in re.findall(r"other \w+ skills", text)]
    case("「其余 N 份」都是当前的", not stale, "、".join(stale))

    # 9. README 说正文全是中文——那就真的是。按字母数和汉字数粗算。
    english = []
    for where, text in list(texts.items()) + [("method/target-format.md", docs["method/target-format.md"])]:
        body = re.sub(r"```.*?```|`[^`]*`|\|[^\n]*", "", text.split("---", 2)[-1], flags=re.S)
        cjk = len(re.findall(r"[一-鿿]", body))
        latin = len(re.findall(r"[A-Za-z]", body))
        if latin > cjk:
            english.append(where)
    case("正文是中文", not english, "还是英文为主：" + "、".join(english))

    # 10. README 不写死自检项数——写死的数字一定会过期。
    case("README 没有写死自检项数", not re.search(r"\d+ 项", readme), "找到：" + "、".join(re.findall(r"\d+ 项", readme)))


if __name__ == "__main__":
    raise SystemExit(main())
