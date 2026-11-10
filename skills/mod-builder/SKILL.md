---
name: mod-builder
description: 玩家的 AI 定制这一局的规范：存档 mods 层能放什么、怎样做自定义命令和面板、哪些事引擎不允许。玩家提出新玩法、新界面、新机制时使用。
---

# 定制这一局 (Mod Builder Skill)

玩家想要什么，先想怎么做到，不要先说做不到。改动写进 `saves/<存档>/mods/`：它覆盖世界包，但不修改世界包，所以这一局怎么改都不影响其他存档。

## 怎么选做法

| 玩家想要 | 做法 |
| --- | --- |
| 新机制（饥饿值、好感事件、炼丹、声望系统……） | 在 `mods/rules.md` 写规则，数值放进主角卡或 `now.md` 的自定义字段，由主持人执行 |
| 一个主动发动的能力 | `mods/commands/<名字>.md`，需要的话配快捷按钮 |
| 文风、世界观补充 | `mods/style_pack.md`、`mods/setting.md` |
| 幕后怎么运转 | `mods/backstage.md` |
| 换配色、字体、对话框样式、状态栏 | `update_world_ui` |
| 地图、关系网、技能树、小游戏，任何新界面 | 自定义面板 `mods/panels/<id>.html` |

很多机制不需要界面。先用规则和命令做到，玩家想看得更直观时再加面板。

调教不只是定制界面：玩家也会要求改这一局的世界本身（主角是什么样的人、某个人物怎么对主角、剧情接下来往哪走）。这些是**这一局的世界状态**，不放 `mods/`，直接改存档里对应的文件，界面上有的地方玩家马上就能看到：

| 玩家说的 | 改这个文件 | 界面上在哪 |
| --- | --- | --- |
| 主角的性格、喜好、感情取向、习惯、待人和说话方式（心性） | `player/persona.md` 对应小节；玩家亲自定的写在小节最前面，标明“玩家亲定” | 心性 |
| 主角的境界、属性、伤势、物品、技能 | `player/sheet.md` | 修为 / 角色状态 |
| 主角知道的事、认识的人、目标 | `player/journal.md`（按日志卡的写法：每条一句话，每栏不超过 8 条） | 行囊手记 |
| 某个人物的性格、态度、和主角的关系 | `world/npcs/<id>.md` | 不显示 |
| 剧情节奏、接下来想看到的事、想淡化的线 | `world/director.md` 的“当前节奏”“近期计划” | 不显示 |
| 世界常识、某个地方的样子 | `world/lore.md`、`world/locations/<id>.md` | 不显示 |
| 玩家亲定、任何时候都成立的世界事实（身世、世界规则上的设定） | `world/canon.md` | 不显示，主持人每轮都会读 |

改这些文件用一次 `python engine/tools/apply.py --save <存档名> --json '[…]'` 提交（会校验，出错自动恢复），不要用 Edit 或 Write 直接改。改了人物或剧情，不要剧透玩家还不知道的秘密；`world/secrets/` 只在玩家明确要求时才动。

主角卡和 `now.md` 可以加任意字段，校验器不管它不认识的字段。要显示在状态栏，把字段配进 `ui_schema.player_status` 或 `world_status`。

## mods 能放什么

```text
saves/<存档>/mods/
├── pack.yaml            只读 ui_schema，与世界包合并
├── theme.json           与世界包主题深度合并
├── setting.md rules.md protagonist.md style_pack.md backstage.md   追加在世界包文档之后
├── commands/<名字>.md   命令，写法见 pack-designer，优先于世界包的同名命令
├── panels/<id>.html     自定义面板
└── data/<id>.json       面板数据
```

每次写入都会校验，不通过就恢复原样，把错误告诉你；修好后再写。改动随存档快照保存，玩家撤回回合时会保留。玩家可以在调教窗的“本存档的定制”里单独移除任何一项。

合并规则：字典逐项合并；带 `id` 的列表按 `id` 合并（如 `panels`）；其他列表整体替换（改 `player_status` 要写完整列表）。给世界包的面板写 `{id: xxx, enabled: false}` 可以在这一局隐藏它。

## 自定义面板

面板是一个单文件 HTML，显示为右侧状态面板的一个页签。在 `ui_schema.panels` 里声明：

```yaml
panels:
  - id: map                       # 对应 panels/map.html
    label: 地图
    icon: 🗺️
    gm_note: 主角到达新地点时，在 nodes 里加一个地点   # 可选，有动态数据时才写
```

用 `write_mods` 一次写完文件和声明。面板里可以用任何 HTML、CSS、内联 JS、内联 SVG、Canvas。

### 面板 API

```html
<div id="app"></div>
<script>
  RPG.onState(function (state) {
    // 首次加载和每回合结束都会调用
    document.getElementById("app").textContent = state.player.name;
  });

  // 把文字填进玩家的输入框，玩家确认后才发送
  // RPG.fillInput("/tick 7 ");
</script>
```

`state` 的内容（都是玩家本来就能看到的）：

| 字段 | 内容 |
| --- | --- |
| `world` | `date` `day` `season` `location` `mood` |
| `player` | `name` `rank` `affinity` `aptitude` `core_engine` `attributes` `skills` `gear` `abilities` `items` `currency` `reputation_items` `status_items` |
| `present_npcs` | 在场人物的明面信息 |
| `journal` | 日志卡分栏，每项 `{id, title, icon, content}` |
| `persona` `milestones` | 性格、重要时刻的文本 |
| `recent_turns` | 最近 3 轮的 `{input, story}` |
| `rules` | `ranks` `levels` `attributes` |
| `data` | 这个面板自己的 `data/<id>.json`，没有时为 `null` |

样式用这些 CSS 变量，就能自动跟随主题和氛围：`--bg-main` `--bg-panel` `--bg-card` `--bg-input` `--text-main` `--text-muted` `--accent-primary` `--accent-secondary` `--accent-warn` `--accent-danger` `--accent-ok` `--border-color` `--on-accent` `--font-body` `--font-heading` `--font-mono`。

### 面板的限制

面板运行在浏览器沙箱里：

- **不能联网**：`fetch`、外部图片、脚本、字体都会被拦截。所有资源都内联，图片用 `data:` 或 SVG。
- **读不到**本页面、存档文件和浏览器存储，也不能弹窗。想保存内容，写进 `data/` 由主持人维护。
- **不能跳转**：面板把自己跳到别的地址会被立即卸载。卸载前那次请求拦不住，这也是面板只能拿到玩家可见数据的原因之一。
- 单个文件不超过 256KB，尽量做小。

### 面板数据

有动态内容时（比如地图上去过的地点）：

1. 用 `write_mods` 写初始的 `data/<id>.json`。
2. 在声明里写 `gm_note`，说清楚什么时候更新什么。
3. 主持人会在回合回写里用 `{"op": "set_panel_data", "panel": "<id>", "data": {...}}` 更新。

**面板数据玩家全部可见。** 不要放主角不知道的内容；隐藏真相继续留在 `world/secrets/`。

## 引擎的底线

这些不能改，也不要绕过：

- 不修改 `engine/`、`web/`、`skills/` 和世界包（玩家明确说要改世界包本身时除外）。
- 宏观布局固定：顶部栏、世界横幅、故事区、状态面板、调教窗、输入台的位置不变。新界面放进面板。
- 回合和存档的回滚机制不变；改存档用回写操作。
- 不向玩家泄露隐藏内容。

## 确实做不到时

比如需要定时触发的后台任务、需要联网的功能。先想有没有替代做法，再调用 `request_engine_extension` 记录玩家想要什么、卡在哪里，然后如实告诉玩家目前做不到，并说明替代做法。不要假装做到了。
