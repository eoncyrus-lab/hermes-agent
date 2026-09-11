# 情圣 / romance-master

一个**基于证据**的情感顾问。它读你给它的材料，为每个人维护一份有出处的档案，
判断现在是什么局，给出下一步——并且从实际发生的事里学习。

它不是一个 PUA 工具箱。区别不在语气，在**规矩**：那种工具需要做的事——从一个人
的性格推断她想要什么、给出证据撑不起的把握、在对方不适时继续优化「进展」、把在
一个人身上奏效的套路搬到另一个人身上——在这里会被**拒绝执行**。拒绝它的是一个
程序，不是一段提示词。

---

## 它现在是什么形态

这个仓库**曾经**是一个完整的后端：Python + FastAPI + arq worker + Telegram bot +
微信通道，Postgres 16 + pgvector，7 个数据库角色，行级安全，8 个迁移，339 个单元
测试和一套评测。

现在它是**十份 skill 加一个编排它们的 agent**，没有服务、没有数据库、没有依赖。
规矩没有变，执行规矩的地方变了——这个取舍下面单独讲，因为它是这个项目最需要被
看清的地方。

引擎那一版完整保留在 git 历史里，最后一个提交是 `f412197`：

```bash
git show f412197 --stat
git checkout f412197 -- .        # 想全部拿回来的话
```

## 十三份 skill

`romance-master` 是**编排者**：它不装方法论，它判断现在是什么局面，把对的那几份
调进来。其余十二份是知识，各自独立、按需加载。

```
skills/romance-master/          编排者。人设、流程、编排表、三条硬规矩
  SKILL.md
  scripts/record.py             写档案。会**拒绝**违规写入
  scripts/check.py              查档案。会揪出手改出来的错
  targets/<名字>.md              每个人的档案——它的长期记忆（默认位置）
skills/method/                  第一原则怎么落地：八条规矩 + 档案文件结构
skills/reasoning/               从证据推到结论而不发明东西（横切）
skills/communication/           一句话字面之下在做什么，以及怎么接
skills/dating-core/             早期推进：读兴趣、升级、邀约、被拒之后
skills/flirting/                调情的几种模式，以及怎么判断有没有落地
skills/psychology/              用来生成假设的心理学（不做诊断）
skills/relationship-science/    什么真正预测关系能否长久
skills/intimacy/                性的沟通层：欲望、契合度、怎么开口、不匹配
skills/technique/               身体上的做法：手、口、进入、姿势、玩具、润滑、疼
skills/kink/                    小众性癖与权力交换：协商、安全、真实风险、drop
skills/erotic/                  情色创作：文爱、语爱、幻想、场景、跟真实伴侣升温
skills/philosophy/              战术问题底下的价值问题
```

前八份的正文是英文（引擎时代原样继承）；`intimacy` 之后的四份是中文。

编排表（在 `skills/romance-master/SKILL.md` 里）长这样——它是引擎时代
`skills.py` 里 `INTENT_SKILLS` 那张路由表的直系后代：

| 局面 | 加载 |
|---|---|
| 该怎么回 | `communication` → `flirting` |
| 这是什么意思 | `psychology` → `communication` |
| 要不要更进一步 | `dating-core` → `flirting` |
| 关系出了状况 | `communication` → `relationship-science` |
| 性与亲密的沟通 | `intimacy` → `communication` |
| 具体怎么做 | `technique` → `intimacy` |
| 性癖 / 权力交换 | `kink` → `intimacy` |
| 情色创作 | `erotic` |
| 我自己的问题 | `psychology` → `philosophy` |

## 装上

十三份 skill 通过 preset 的 `skill-filesystem` **`customSkillDirs`** 挂载。这一步不能省：
默认 skill 根只有项目根和用户根（`<projectRoot>/.dsh/skills`、`~/.dsh/skills` 等），
**preset 自己的 `skills/` 不在其中**——已发布的 `cordis` preset 也是显式配了这项才拿到
它自带的两份 skill 的。

```yaml
- id: skill-filesystem
  name: '@deepseek-ai/dsh-skill-filesystem'
  config:
    customSkillDirs:
      - /path/to/this/repo/skills
```

这样挂上来的 skill **只对该 preset 的会话可见**——别的 preset 不会在自己的目录里看到
这十三份东西。

`romance-master` 这个 agent preset 已经装在这个部署里（`~/.dsh/.agent-presets/romance-master/`），
在 GUI 的 preset 选择器里叫「情圣」。它是从 `standard` 复制后逐行删出来的：留下 shell、
文件工具、skill 加载器、提问工具和压缩；删掉子代理、工作流、计划模式、待办、后台任务
和网页检索。

**代价写明白**：`customSkillDirs` 里是一个绝对路径，把 preset 钉在这个仓库的位置上。
仓库搬走，十三份 skill 会**静默消失**（目录不存在属于合法空状态，不报错）。搬了就去改
那一行。

档案默认写在 `skills/romance-master/targets/`。要把它挪出仓库（私人数据不该和会被推到
远端的技能代码混在一起），设 `ROMANCE_TARGETS`：

```bash
export ROMANCE_TARGETS=~/.dsh/romance-master/targets
```

## 性、性癖与情色内容

这一类内容在这里是**在范围内**的、明确欢迎的，并且有四份 skill 专门做这件事：

- **`intimacy`** —— 沟通层：欲望不匹配、怎么开口、边界、避孕、反应性欲望与欲望落差。
- **`technique`** —— 身体层：手、口、进入、姿势（按角度/深度/阴蒂接触/主动权/体力拆解，
  而不是罗列体位）、节奏、玩具（含材质与润滑剂相容性——**硅基润滑会溶掉硅胶玩具**）、
  肛交的硬安全要求、疼痛与什么时候该看医生。
- **`kink`** —— 小众性癖与权力交换：限界与安全词、RACK 风险框架、束缚的神经损伤、
  拍打的安全区与禁区、sub drop / top drop。对**窒息**这类真正致命玩法给的是诚实的
  风险说明，不是含糊过去。
- **`erotic`** —— 创作：文爱、语爱、幻想、场景、情色写作的手艺，以及怎么在聊天里
  跟真实伴侣升温。

有一条区分必须说清楚，因为把两件事混起来会得出完全错误的结论：

- **不可推断**——关于**某个具体的人**的事实：她的身体、她的性偏好、她的健康状况、
  她同不同意。这些只能来自她本人的原话，不能从穿着、职业、性格、聊天风格推断。
  脚本会拒绝这类写入。
- **可以谈**——其余全部。包括怎么做到、用什么、想玩什么、想写什么。

`intimacy/` 把「临床式的疏离」和「委婉语」列为**失败模式**：用户问怎么跟伴侣说自己
想要什么，而回答说不出他想要什么，这个回答就是失败的。冷淡和含糊不是稳妥。

### 唯一一条硬边界：虚构归虚构，档案归档案

幻想、文爱、角色扮演、让 agent 扮演伴侣——**全部允许**，随便写。但：

> **创作里发生的任何事，一个字都不许进 `targets/`。**

文爱里"她说她想要"不是证据；场景里"她答应了"不是同意；扮演她说的话不是关于她的信息。
档案只记录**真实发生过**的事。拿不准一条信息是来自真实材料还是来自刚才的虚构——
它就不是证据，宁可漏记。

这道墙是"完全放开"能成立的原因：情色那一侧毫无保留，而"没有证据就不下结论"这条
根基不被污染。没有它，写过一个场景之后 agent 就再也分不清自己知道什么了。

另一条没有商量余地的是**未成年**：涉及真实未成年人的性/约会内容一律停止。

## 用户自己的偏好

关于**用户本人**的东西不需要证据纪律——他是自己喜好的唯一权威。用 `我` 这个档案装，
下次就不用重新问一遍他的口味：

```bash
python3 scripts/record.py new 我 --retention-days 3650
python3 scripts/record.py explicit 我 --axis sexual.偏好 --state 明确 --quote "我喜欢慢的"
```

注意区分：他说的是**他自己** → 记进 `我`；他说的是**她** → 只有她本人说过才能记。

## 用

在会话里正常说话就行——「她昨天这么回我，我该怎么接」。agent 会加载 skill 并按里面的
流程走。也可以直接敲脚本：

```bash
cd /path/to/this/repo/skills/romance-master

python3 scripts/record.py new 小美                     # 建档，带保留期
python3 scripts/record.py evidence 小美 --source 原文 --text "她主动问我周末有没有空"
python3 scripts/record.py claim 小美 \
    --axis attraction.romantic --value 0.6 --evidence e1,e2
python3 scripts/record.py show 小美                    # 手上到底有什么
python3 scripts/check.py                               # 交付前自检
```

只看标准库，`python3` 3.8+ 就行——没有虚拟环境，没有 pip。

## 方法

八条规矩，写在 `skills/method/SKILL.md`：

1. **没有证据就没有结论。** 一条结论必须引用真实存在的证据 id。
2. **置信度由证据条数封顶。** 1 条最多 0.45，2 条 0.60，3 条 0.70，6 条以上 0.85。
3. **重复不算新证据。** 同一句话记两遍仍然是一条观察。
4. **禁区不可推断。** `sexual.` `kink.` `body.` `health.` `consent.` 只能来自明确陈述。
5. **同意是个枚举，不是感觉。** `同意` / `拒绝` / `硬边界` / `未知`。沉默不是同意，
   以前同意过也不是同意。
6. **预测先冻结，事后不改。** 否则「我早说过了」永远成立。
7. **不舒服压过一切。** 不管对方回复得多快。
8. **人和人之间隔离。** 一个人一份档案，混了会被 `check.py` 揪出来。

这些不是建议——`record.py` 会拒绝违反它们的写法：

```
$ python3 scripts/record.py claim 小美 --axis attraction.romantic --value 0.8 --evidence e1
拒绝：0.80 超过了 1 条观察撑得起的上限（上限 0.45）。要么把值降下来，要么再拿证据来。

$ python3 scripts/record.py claim 小美 --axis body.appearance --value 0.5 --evidence e1
拒绝：「body.appearance」是受保护的命名空间，证据再多也不能推断。
```

---

## 这个形态放弃了什么

必须说清楚，因为它是这个项目唯一真正重要的取舍。

引擎那一版把规矩放在**数据库约束**和 **pydantic 校验器**里，两层独立，任何一层都
不是唯一挡住坏结论的东西。README 当时写着：这些事「由 schema 层拒绝，而不是靠提示词
劝阻」。

**这句话在现在的形态下不再成立。** 现在规矩由 `scripts/record.py` 执行，而它是一段
**被要求去运行**的脚本。一条数据库约束无法被跳过；一段脚本可以——不运行它，或者绕开
它直接改 markdown 文件。`check.py` 是事后审计，而它的效力完全取决于「到底跑了没有」。

诚实的说法是：

- **活下来的**：规矩本身，而且现在它们**可执行、可测试、会给出人能读懂的理由**。
  被拒绝时 agent 拿到的是一句能转述给你的话，而不是一段 traceback。
- **丢掉的**：不可跳过性。多用户隔离（原来的行级安全）。微信和 Telegram 通道。
  用户自带 API key。评测套件和标定。第三方记录的保留期强制执行（现在靠
  `check.py` 报告，不靠数据库拒绝）。
- **不变的**：这是给**一个人**用的工具。多人共用会退回到「一个人一份档案」的
  文件隔离，而那比行级安全弱得多。

如果这几条里有哪一条对你重要，引擎那一版在 `f412197`，它是完整可运行的。

## 它不该做什么

- **不替你做决定。** 它给判断和选项，你选。尤其不催表白、不催分手、不催摊牌。
- **不做心理诊断。** 「回避型依恋」描述行为可以，贴标签不行。它不是医生。
- **不用套路话术。** 它给的是基于**这个人**的具体做法。
- **不把虚构当事实。** 文爱和角色扮演随便写，但创作里的内容既不能进档案，也不能
  被当成关于某个真人的信息。
- **不教怎么绕过拒绝。** 「欲拒还迎」不是可以推断的东西——除非事先明确约定过，
  否则"不要"就是不要。
- **不碰未成年人。** 出现这种信号就停下，并说明原因。这是唯一一条没有例外、
  也没有"只是幻想"空间的边界。

---

## 关于隐私

`skills/romance-master/targets/` 里是关于真人的私人材料。默认它在仓库目录里，而仓库
会被推到远端——**这是默认设置，不是推荐设置**。真正开始用之前，把
`ROMANCE_TARGETS` 指到仓库外面去。它已经在 `.gitignore` 里（第 11 行），但那是一道
防线，不是你放心的理由。

第三方记录默认带一年保留期，`check.py` 到期会报错。这是这套东西能给的最低限度的
体面：你可以给一个人建模，但得在上面写个截止日期。
