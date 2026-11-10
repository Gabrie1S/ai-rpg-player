# 通用数据格式规范（Universal Schema Specification）

所有存档数据位于 `saves/<存档名>/` 下；所有独立世界观包位于 `packs/<世界包ID>/` 下。
`engine/tools/validate.py` 依据本规范与存档内的 `world/rules.yaml`（特别是其中的 `schema_mapping`）进行动态强校验；前端与状态栏依据 `packs/<世界包ID>/pack.yaml` 中的 `ui_schema` 动态渲染世界状态栏、玩家状态栏与角色日志卡分栏。

---

## 一、 通用约定

1. **一个实体一个文件**：YAML Frontmatter（`---` 包裹）存放结构化机器可读字段，Markdown 正文存放叙事描写。
2. **全局唯一 ID**：`前缀_拼音或英文蛇形`，文件名必须等于 `ID + .md`。
   - NPC：`npc_old_hawk`
   - 地点：`loc_rust_dock`
   - 势力：`fac_titan_sec`
   - 事件：`evt_dock_raid_1`
   - 玩家角色固定为：`player`
3. **引用一律用 ID**：所有关联字段（如 `location`、`faction`、`leader`、`present`、`participants`）一律填 ID，不填显示名称。
4. **时间**：整数游戏日（`day`），第 `0` 天为开局。显示给玩家时由 `clock.py` 按 `world/world.yaml` 的历法配置换算，或称“第 N+1 天”。
5. **`locked: true`**：玩家锁定标记。凡带此标记的文件或事件，AI 与 `world-sim` 均不得擅自篡改，仅玩家使用 `/gm` 可修改。
6. **态度数值范围**：一律为 `-100 ~ 100`。

---

## 二、 `packs/<id>/pack.yaml` 与 `theme.json` — 独立世界包配置与定制化 UI 契约

每个世界观包是 `packs/` 下的一个独立文件夹（拖入即识别，删除文件夹即卸载，嵌套一层子文件夹可由 `pack_io.py flatten` 一键扁平化）。
系统界面遵循**「固定系统壳 vs 定制化世界 UI」**架构边界：顶部全局配置栏（`.system-topbar`）与最右侧独立场外调教窗（`#tuner-dock`）固定不变；世界横幅、像素天际线、状态控件、角色卡模块显隐、HUD 标签命名与日志分栏均由 `pack.yaml` 的 `ui_schema` 与 `theme.json` 动态驱动（详见 `skills/ui-designer/SKILL.md`）。

### 1. `pack.yaml` 示例与 `ui_schema` 6 种自适应状态控件

支持的 `widget` 控件类型（仅限以下 6 种）：`badge`（胶囊标签）、`bar`（进度条）、`fraction`（双数值比值，如 `65/80`）、`meter`（带 `warn_at`/`danger_at` 阈值的警戒条）、`tags`（词条芯片组）、`kv`（键值行）。

```yaml
id: cyberpunk
name: 霓虹深渊·2084
genre: 赛博朋克都市雇佣兵
theme: cyberpunk-neon

ui_schema:
  tagline: "霓虹照不到的锈铁下城，每一克义体都标好了价码"
  hud_tabs:
    scene:      {label: "街头手札", icon: "📡"}
    sheet:      {label: "义体档案", icon: "🦾"}
    persona:    {label: "行为侧写", icon: "🧠"}
    milestones: {label: "传奇履历", icon: "🏆"}
  world_status:
    - {key: day, label: 天数, icon: "📅", widget: badge, tone: accent}
    - {key: date, label: 历法日期, icon: "🕒", widget: badge}
    - {key: loc, label: 当前坐标, icon: "📍", widget: badge}
    - {key: alert_level, label: 街区警戒, field: alert_level, icon: "🚨", widget: meter, max: 100, warn_at: 50, danger_at: 80, default: 15}
  player_status:
    - {key: rank, label: 街头阶位, field: street_tier, icon: "⚡", widget: kv}
    - {key: core_engine, label: 神经操作系统, field: os_core, icon: "🧠", widget: kv}
    - {key: chrome_load, label: 义体神经负荷, field: chrome_load, icon: "🔥", widget: bar, max: 100, unit: "%", color: secondary, default: 24}
    - {key: affinity, label: 义体与战术相性, field: chrome_affinity, icon: "🔌", widget: tags}
    - {key: currency, label: 信用点 (eb), field: eddies, icon: "💳", widget: kv}
  character_sheet:
    attributes_title: "核心机能参数"
    # 合法模块 id 仅限：status_widgets, attributes, core_engine, affinities, aptitude, abilities, skills, gear, reputation, inventory
    sections:
      - {id: status_widgets, title: "核心体征与负荷", visible: true}
      - {id: attributes, title: "核心机能参数", visible: true}
      - {id: affinities, title: "义体与战术相性", visible: true, hide_empty: true}
      - {id: abilities, title: "专长协议", visible: true, hide_empty: true}
      - {id: skills, title: "战术与黑客技能", visible: true, hide_empty: true}
      - {id: gear, title: "植入义体与武装", visible: true}
      - {id: reputation, title: "街头声望记录", visible: true, hide_empty: true}
      - {id: inventory, title: "随身芯片与备注", visible: true}
  journal_sections:
    - {id: contracts, title: "当前委托与目标", icon: "📋", hint: "正在跟进的街头雇佣委托与阶段目标"}
    - {id: contacts, title: "认识的人脉", icon: "📇", hint: "打过交道的中间人、义体医生、黑客与帮派人物"}
    - {id: intel, title: "街头风声与情报", icon: "📡", hint: "从酒吧、暗网终端或路人口中听到的局势异动"}
    - {id: mysteries, title: "追查中的疑点", icon: "🔍", hint: "尚未查明真相的线索断层与幕后黑手痕迹"}
```

### 2. `packs/<id>/theme.json` — 配色、字体、对话框样式、四周像素绘画与动态氛围预设

`theme.json` 驱动世界视觉皮肤（保持宏观布局固定不变），支持 `css_vars`、`typography`、`dialogue_box`、`pixel_frame`、`mood_presets`（随剧情动态切换主题氛围与像素天气）与 `skyline`：

```json
{
  "id": "cyberpunk-neon",
  "name": "霓虹深渊 (Cyberpunk)",
  "css_vars": {
    "--bg-main": "#070913",
    "--bg-panel": "#0e1324",
    "--bg-card": "#141c30",
    "--bg-input": "#0a0e1a",
    "--text-main": "#e4ecf7",
    "--text-muted": "#8a9bb5",
    "--accent-primary": "#00f0ff",
    "--accent-secondary": "#ff2a85",
    "--border-color": "#23304c",
    "--dialogue-color": "#7ee7ff"
  },
  "typography": {
    "font_body": "'Inter', 'PingFang SC', 'Microsoft YaHei', sans-serif",
    "story_font_size": "0.98rem",
    "story_line_height": "1.88"
  },
  "dialogue_box": {
    "style": "cyber-chamfer",
    "max_width": "740px",
    "gutter_x": "104px",
    "corner_ornament": "◢",
    "quote_style": "bracket-glow"
  },
  "pixel_frame": {
    "enabled": true,
    "pixel_scale": 4,
    "scene": "cyber_alley",
    "weather": "neon_rain",
    "palette": {".": null, "k": "#0b0f1a", "c": "#00f0ff", "m": "#ff2a85"},
    "sprites": []
  },
  "mood_presets": {
    "default": {"label": "🌧️ 常态·霓虹夜雨", "weather": "neon_rain"},
    "combat":  {"label": "🔥 交火·高危警报", "weather": "embers", "css_vars": {"--accent-primary": "#ff3b5c"}},
    "mystery": {"label": "🌫️ 潜行·暗网迷雾", "weather": "fog", "css_vars": {"--accent-primary": "#8b5cf6"}},
    "rest":    {"label": "☕ 休整·安全屋微光", "weather": "fireflies", "css_vars": {"--accent-primary": "#f59e0b"}}
  },
  "skyline": {
    "style": "urban",
    "sky_top": [8, 10, 26],
    "sky_bot": [28, 16, 52],
    "far": [22, 30, 62],
    "near": [14, 18, 38],
    "ground": [10, 12, 24],
    "accent1": [0, 240, 255],
    "accent2": [255, 42, 133],
    "highlight": [255, 214, 70]
  }
}
```

---

## 三、 `world/world.yaml` — 世界与历法配置

```yaml
name: 霓虹暗流
pack: cyberpunk          # 所使用的世界包 ID (对应 packs/<pack>/)
genre: 赛博朋克
calendar:
  format: modern         # traditional (传统纪年+月日) | modern (星历/公历纪年+月日)
  era: NC.               # 纪年前缀，如 "NC." 或 "星历"
  start_year: 2084       # 第 0 天所在年份
  start_month: 11
  start_day: 1
  months_per_year: 12
  days_per_month: 30
tick_days: 7             # 幕后常规推演间隔（天）
snapshot_days: 90        # 世界快照间隔（天）
```

---

## 四、 `world/rules.yaml` — 规则表驱动配置（Schema-Driven Rules）

`rules.yaml` 既是世界规则的数值表，也是 `validate.py` 的动态 Schema 定义表。通过顶部 `schema_mapping`，同一套引擎可以校验任何题材的角色卡：

```yaml
schema_mapping:
  rank_field: street_tier           # 阶位/职阶字段名（如 street_tier, rank, sequence）
  affinity_field: chrome_affinity   # 先天适性/相性字段名（自动支持 <field>_active 与 <field>_shown）
  aptitude_field: aptitude          # 潜能字段名（自动支持 <field>_shown）
  engine_field: os_core             # 唯一主引擎字段名（如 os_core, core_engine, pathway）
  currency_field: eddies            # 货币字段名（如 eddies, credits, gold）

levels: [Lv1, Lv2, Lv3, Lv4, Lv5, Lv6, Lv7, Lv8, Lv9]
ranks:
  - {name: 平民, levels: false, attr_range: [1, 3], lifespan: 75}
  - {name: 街头小子, levels: true, attr_range: [2, 5], lifespan: 80}
  # ...
```

### 核心字段通用规则
- **阶位（`rank_field`）**：写法为 `大阶位名 + 等级`（如 `街头小子Lv3`）；`levels: false` 的基础阶位不带等级（如 `平民`）。
- **适性/相性（`affinity_field`）**：`属性 → 等级` 的字典，如 `{黑客: 军规, 电磁: 改装}`；无适性写 `{}`。
  - **隐藏与觉醒层（可选）**：`<affinity_field>_active` 表示当前已生效的部分（规则计算以此为准，属性须在真实适性内且等级不超过真实等级）；`<affinity_field>_shown` 表示外人或常规检测看到的表象。
- **潜能（`aptitude_field`）**：如 `{反射神经: 中, 算法直觉: 上}`，键来自 `aptitude_axes`，值来自 `aptitude_grades`。支持 `<aptitude_field>_shown`。
- **唯一主引擎（`engine_field`）**：角色同时只能搭载一个主引擎，格式为 `{name, element, grade, orientation, limit}`，`limit` 为最高可支撑的大阶位名；无主引擎写 `null`。
- **基础属性（`attributes`）**：键来自 `rules.yaml` 的 `attributes` 列表，数值必须落在该角色当前大阶位的 `attr_range` 区间内（特例可加 `attr_override: true`）。
- **技能（`skills`）**：列表，每项 `{name, level, element?, type?, grade?}`。
- **装备（`gear`）**：列表，每项 `{name, tier, bound?}`。
- **特殊能力（`abilities`）**：列表，每项 `{name, level, notes}`。

---

## 五、 `world/now.md` — 当前场景、动态 UI 氛围与回合状态机

```yaml
---
day: 0
location: loc_rust_dock
present: [npc_old_hawk]
last_tick_day: 0
last_snapshot_day: 0
frozen: false
volume: 1
ui_mood: default         # 对应 theme.json -> mood_presets 的情境键名（如 default / combat / mystery / rest），随剧情自动切换配色与像素天气
recent_challenges: []    # 引擎自动提取并记录最近【难关】判定结果，用于防放水天平校准
---
```
正文固定三节：`## 当前场景`、`## 进行中的事`、`## 待处理`。

---

## 六、 分层切片索引与实体记忆区（防上下文肥胖铁律）

- **三级轻量目录索引（一行一条，按需精读）**：
  - `world/index.md`（由 `index.py` / `apply.py` 自动维护）：所有人物、地点、势力一行一条，并按最近见面排序记录“主角见过的人”。
  - `world/design/目录.md` 与 `world/secrets/目录.md`（由 `split_md.py --index` / `apply.py` 自动维护）：世界设计（核心循环、分卷规划、序章剧本等）与幕后真相（深层阴谋、暗流布局、主角暗线、揭示计划等）必须按主题拆成 **≤ 12KB** 的独立小文件，禁止糊成单个超大文件。
  - `world/stage.md`（本阶段精准切片清单）：包含 `## 当前阶段`、`## 台前必读`（≤ 6 个文件）、`## 用到时再读` 与 `## 用来埋伏笔的秘密`（仅列当前阶段埋伏笔需要看的那 1～2 个秘密切片）。
- **实体与日志区**：
  - `world/locations/<id>.md`、`world/factions/<id>.md`、`world/npcs/<id>.md`、`world/events/timeline.yaml`
  - `world/canon.md`（玩家亲定事实）、`world/director.md` & `world/hooks.md`
  - `player/sheet.md`、`player/journal.md`（按世界包 `ui_schema.journal_sections` 分栏的角色日志卡）、`player/persona.md`
  - `log/story/第X卷/NNNN-日期.md`、`log/milestones.md`、`log/summaries.md`、`log/snapshots/`、`log/changelog.md`、`log/dice.log`

## 世界包的其他文件

世界包的完整文件清单见 `skills/pack-designer/SKILL.md`。这里只列引擎读取的字段：

- `pack.yaml` 的 `calendar.month_names`、`calendar.day_names`：自定义月份名和日期名。
- `pack.yaml` 的 `setup`：开局设置清单，决定问玩家什么、答案写到哪、要不要进入定制界面（`engine/tools/pack_setup.py`）。
- `pack.yaml` 的 `ui_schema.quick_commands`：输入台上的快捷按钮，每项 `{label, command, hint}`。
- `commands/<名字>.md`：世界包命令，优先于同名通用命令；YAML 头写 `archive: true` 时结果算作一个回合。
- `backstage.md`：只注入幕后和创世向导的提示词。
- `templates/world/`：新建存档时覆盖到存档里。

## config.yaml 的 back_driver

幕后 world-sim 使用的模型，属于玩家配置，世界包不能设置：

- `inherit_front: true`：和台前相同（默认）。
- `inherit_front: false` + `same_endpoint: true` + `model`：沿用台前的接口和密钥，只换模型。
- `inherit_front: false` + `same_endpoint: false` + `api_base` / `api_format` / `api_key` / `model`：单独的接口。接口地址不同时不会使用台前的密钥。

## saves/<存档>/mods/ — 存档定制层

结构与世界包相同，只写想改的部分，覆盖顺序：引擎默认 < 世界包 < 存档 mods。规范见 `skills/mod-builder/SKILL.md`，工具见 `engine/tools/mods.py`。
