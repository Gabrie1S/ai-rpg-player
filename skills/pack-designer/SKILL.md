---
name: pack-designer
description: 世界包设计规范：一个世界包应该包含哪些文件、每个文件写什么、给谁看、怎么放自定义机制。设计、生成、审查或把旧项目改造成世界包时使用。
---

# 世界包设计规范 (Pack Designer Skill)

世界包（`packs/<pack_id>/`）是一个题材的全部内容。引擎、玩家配置、世界包、存档四者互相独立：

| 层 | 位置 | 内容 | 谁改 |
| --- | --- | --- | --- |
| 引擎 | `engine/` `web/` `skills/` | 通用流程，不含任何题材内容 | 引擎开发者 |
| 玩家配置 | `config.yaml` | 模型、密钥、提示词模板 | 玩家 |
| 世界包 | `packs/<id>/` | 这个题材的规则、设定、文风、界面 | 世界包作者 |
| 存档 | `saves/<名字>/` | 一局游戏生成的世界和进度 | 游戏运行时 |

**热插拔的底线**：把世界包文件夹放进 `packs/` 就能用，删掉后引擎恢复原样，已有存档靠内置设定继续运行。为此：

- 服务器上不运行世界包的代码。自定义机制写成规则文档和命令，由主持人执行；自定义界面写成面板，在浏览器沙箱里运行（见 `mod-builder`）。
- 世界包不改引擎文件，也不带模型、密钥这类玩家配置。
- 引擎的扩展点不够用时，给引擎加一个通用扩展点，而不是在某个包里打补丁。

**省上下文**：世界包文档每轮都会发给台前。`rules.md` 这类长文档，把每轮都要用的放前面，后面加一行 `<!-- 台前常驻到这里 -->`，其余章节只列目录，用到时才读。挂了 `optional_skills` 的能力，在对应命令文件的 YAML 头写 `triggers: [关键词]`，玩家说到时才带上规则。

## 完整度：按项声明谁来决定

世界包可以很完整，也可以留很多空白，不分等级。在 `pack.yaml` 的 `setup` 里逐项声明：

```yaml
setup:
  - {id: name, label: 主角的名字, mode: ask, required: false, target: sheet.name, hint: 留空由 AI 起名}
  - {id: identity, label: 开局身份, mode: ask, type: choice, options: [甲, 乙], target: canon}
  - {id: tone, label: 基调, mode: default, value: 黑暗, target: note}
  - {id: era, mode: fixed, value: 战后第三年, target: note}
  - {id: ui, mode: default}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `ask` 问玩家；`default` 有默认值，玩家可改；`fixed` 作者定死，玩家看不到 |
| `type` | `text`、`textarea` 或 `choice`（配 `options`） |
| `required` | `ask` 默认必填；`false` 时留空就交给 AI |
| `target` | `sheet.<字段>` 写入主角卡；`canon` 写入亲定事实（默认）；`note` 只告诉幕后生成世界时参考 |

保留项 `id: ui` 决定要不要进入定制界面这一步：`ask` 进入；`default` 跳过但可以选择进入；`fixed` 不提示。不写时，`theme.json` 已有对话框或像素画就按 `default`，否则按 `ask`。

没有写进 `setup` 的项由幕后按世界包内容生成。玩家的回答写进存档（`world/setup.yaml`），不写进世界包。终端模式下用 `python engine/tools/pack_setup.py show <包ID>` 查看要问的项，用 `apply <存档名> --json '{...}'` 写入。

## 文件清单

```text
packs/<id>/
├── pack.yaml            必需  元信息、历法、可选技能、界面字段
├── setting.md           必需  世界设定总纲
├── rules.md             必需  给主持人的裁决规则
├── rules.yaml           必需  给程序校验的规则数据
├── protagonist.md       必需  主角的开局身份与待定项
├── style_pack.md        必需  题材文风、人物写法、正反范例
├── theme.json           推荐  配色、字体、对话框、像素画、情境主题
├── backstage.md         可选  只给幕后的规则与开局生成清单
├── commands/<名字>.md   可选  题材专属的斜杠命令
├── panels/<id>.html     可选  自定义面板，声明在 ui_schema.panels（写法见 mod-builder）
└── templates/world/     可选  新建存档时覆盖到存档里的文件
```

运行时缓存 `.slices/` 由引擎生成，不进版本库和导出包，不要手动编辑。

## 每个文件写什么

先按读者分内容：**玩家可以在世界包中心看到 `setting.md`、`rules.md`、`protagonist.md`、`style_pack.md`、`pack.yaml`、`rules.yaml`、`theme.json`，看不到 `backstage.md`、`templates/` 和 `commands/`。** 真正的隐藏真相不要写进玩家能看到的文件。

| 文件 | 写什么 | 不写什么 | 谁会读 |
| --- | --- | --- | --- |
| `setting.md` | 基调、世人皆知的世界结构、力量体系的常识、势力类型、思想立场 | 具体阴谋、幕后黑手 | 台前、幕后、向导 |
| `rules.md` | 判定依据和优先级、结果档位、数值含义、成长与突破、伤势、时间消耗、关系变化、特殊能力规则 | 剧情 | 台前、幕后、向导 |
| `rules.yaml` | `rules.md` 里需要程序校验的部分，两边保持一致 | 说明文字 | 校验器、状态栏、幕后 |
| `protagonist.md` | 主角开局处境、玩家待定项、序章要达到的效果、主角开局知道什么 | 主角自己不知道的身世 | 台前、幕后、向导 |
| `style_pack.md` | 这个题材特有的语言质感、各类人物怎么写、戏剧偏好、避免的套路、玩家校准过的正反范例 | 通用写作规则（已在 `narrative-craft`） | 台前、幕后 |
| `theme.json` | 见 `ui-designer` | 布局改动 | 前端 |
| `backstage.md` | 开局必须秘密生成什么、幕后维护这个世界的专属规则 | 已确定的具体真相（生成后写入存档） | 只有幕后和向导 |
| `commands/*.md` | 一条题材专属能力的完整执行步骤 | 通用流程 | 玩家触发时 |
| `templates/world/` | 不可违背的亲定事实、主角卡初值、设计与秘密文件的空骨架 | 进度数据 | 新建存档时 |

### pack.yaml

```yaml
id: my_pack
name: 显示名称
version: "1.0.0"
genre: 题材
description: 一句话介绍
optional_skills: [causal-foresight]   # 可选，挂载 skills/ 下的通用模块

calendar:
  era: 纪年名
  start_year: 1372                     # 世界已经延续到的年份，不要写 1：世界不是因主角才开始的
  start_month: 1
  start_day: 1
  months_per_year: 12
  days_per_month: 30
  month_names: [一月, 二月]            # 可选，自定义月份名
  day_names: [初一, 初二]              # 可选，自定义日期名

ui_schema:
  tagline: 一句话氛围
  hud_tabs: {}          # scene / sheet / persona / milestones 的标题和图标
  world_status: []      # 顶部世界状态
  player_status: []     # 角色状态控件
  character_sheet: {}   # 角色卡模块的标题与显隐
  journal_sections: []  # 日志卡分栏
  quick_commands:       # 可选，输入台上的快捷按钮
    - {label: 显示文字, command: /命令名, hint: 鼠标提示}
```

### rules.yaml

用 `schema_mapping` 把通用概念映射成题材字段，引擎只认映射，不认具体的词：

```yaml
schema_mapping:
  rank_field: 阶位字段名
  affinity_field: 先天适性字段名
  aptitude_field: 资质字段名
  engine_field: 唯一主引擎字段名
  currency_field: 货币字段名

ranks:
  - {name: 阶位名, levels: false, attr_range: [1, 3], lifespan: 70}
levels: [一级, 二级]        # levels 为 true 的阶位写作「阶位名 + 等级」
attributes: [属性一, 属性二]
attitude_axes: [好感, 信任, 敬畏]
aptitude_axes: [资质一]
aptitude_grades: [下, 中, 上]
affinity_grades: [低, 中, 高]
elements: {basic: [], variant: []}
restrain: {}                # 克制
support: {}                 # 相生
grades: [低阶, 高阶]        # 配合 grade_steps 校验品阶
grade_steps: [下, 中, 上]
orientations: []
skill_types: []
skill_levels: []
gear_tiers: []
difficulty: {普通: 8}
modifier_cap: 6
```

额外的键可以随意加，校验器会忽略，主持人能读到。

**隐藏属性**：真实值与世人所见不同时，写三个字段：`<适性字段>`（真实）、`<适性字段>_active`（当前生效，规则按它算）、`<适性字段>_shown`（界面显示）。资质同理，有 `<资质字段>_shown`。界面只显示 `_shown`。

### commands/<名字>.md

```markdown
---
description: 给玩家和主持人看的一句话说明
argument-hint: <参数提示>
archive: true    # 结果要进入故事记录并影响后续回合时写 true
---

玩家发动某能力：$ARGUMENTS

1. 执行步骤……
```

- 世界包的命令优先于同名通用命令。
- 不写 `archive: true` 时，回复只显示在当前页面，不进存档，后续回合也看不到。
- 终端模式下主持人从系统提示里得知这些命令，所以自然语言触发也要在 `rules.md` 里写明。

### templates/world/

按存档结构放文件，新建存档时覆盖引擎的默认骨架。常用的有：

- `world/canon.md`：玩家亲定、任何时候都不能违背的事实，写 `locked: true`。隐藏真相放这里，台前每轮都能读到，玩家在世界包中心看不到。
- `player/sheet.md`：字段名必须和 `schema_mapping` 对应，初值要落在 `attr_range` 内。
- `player/persona.md`：主角性格记录的分栏。
- `world/design/*.md`、`world/secrets/*.md`：幕后要填写的骨架。单文件不超过 12KB，由幕后填写，目录自动维护。

不提供 `player/journal.md` 时，引擎按 `journal_sections` 生成。

## 把一个已有项目改造成世界包

1. 按上表把旧文档拆开：世界观进 `setting.md`，判定与数值进 `rules.md`，机器数据进 `rules.yaml`。
2. 把文风文档里的通用规则去掉，保留题材部分和所有正反范例：那些范例是真实游玩校准出来的，最值钱。
3. 把主角文档按“玩家能不能看”拆开：能看的进 `protagonist.md`；亲定的隐藏事实进 `templates/world/world/canon.md`；生成要求进 `backstage.md`。
4. 把幕后提示词里题材专属的条目摘到 `backstage.md`，通用条目留给 `world-sim`。
5. 把题材专属命令搬到 `commands/`，把旧的文件路径改成新结构的路径。
6. 用旧项目的真实存档跑一次校验，能过说明 `rules.yaml` 和映射写对了。

## 检查清单

```bash
python engine/tools/pack_io.py doctor <pack_id>
python engine/tools/newsave.py <测试存档名> --pack <pack_id>
python engine/tools/validate.py --save <测试存档名>
```

- [ ] 体检和存档校验都没有错误
- [ ] `setup` 只问真正该由玩家决定的事，亲定事实不重复问
- [ ] 主角卡字段和 `schema_mapping` 一致，初值在阶位范围内
- [ ] 世界包中心能看到的文件里没有隐藏真相
- [ ] 没有引擎内部路径（如旧项目的 `engine/docs/`），文件互相引用用文件名
- [ ] 除 `panels/*.html` 外没有代码，也没有模型名或密钥
- [ ] 删除这个包后，引擎和其他世界包不受影响
