---
name: ui-designer
description: 固定宏观布局下的世界定制化前端 UI 与对话框留白像素绘画技能。保持顶部信息栏与右侧状态栏等总体 Layout 固定不变，按世界观定制配色、字体、对话框样式，并在对话框周围留白区绘制像素风格艺术场景与精灵。
---

# 定制化世界 UI、对话框样式与留白像素绘画技能 (UI & Pixel Frame Designer Skill)

本技能规范了如何在**保持总体宏观布局（Macro Layout）完全固定**的前提下，为不同世界观量身定制**配色、字体、对话框外观样式，以及在故事对话框四周留空区域绘制像素风格插画（Pixel-Art Margin Painting）**。

---

## 1. 核心架构准则：总体 Layout 固定 vs 视觉与像素画自由定制

**绝对禁止修改 `web/static/` 下的引擎前端源码**。所有 UI 定制均通过当前世界包目录下的两个纯数据文件完成：`packs/<pack_id>/theme.json` 与 `packs/<pack_id>/pack.yaml`（`ui_schema`）。

| 维度 | 覆盖范围 | 定制边界 |
| :--- | :--- | :--- |
| **总体宏观布局 (Fixed Macro Layout)** | ① **顶部系统配置栏**（世界包切换、世界包中心、开局向导、存档、导出、驱动设置）<br/>② **顶部世界信息与状态横幅**（标题、天际线、状态胶囊位）<br/>③ **右侧角色与日志固定状态栏**（4 个标准面板区的骨架与宽度）<br/>④ **最右侧独立场外调教窗 (`Tuner Dock`)** 与 **底部行动输入底座** | **位置、分栏骨架与交互入口严格固定**。确保任何题材下玩家的操作肌肉记忆 100% 一致，绝不发生面板错位或找不到按钮。 |
| **AI 自由定制层 (Customizable Styling & Pixel Art)** | ① **全局配色方案 (`css_vars`)** 与 **字体排版 (`typography`)**<br/>② **居中故事对话框样式 (`dialogue_box`)**（边框造型、切角/雕花、底纹透明度、四角装饰符、光晕、人物台词与难关徽章样式）<br/>③ **对话框四周留白像素绘画 (`pixel_frame`)**（左右及上下留空画布的像素建筑/山川/废墟剪影、像素微气候动画、以及 **AI 字符矩阵手绘像素画精灵 `sprites`**）<br/>④ **右侧状态栏内的数据字段映射 (`ui_schema`)**（进度条/双数值/警戒条等数据绑定） | **完全开放 AI 发挥**。修改落盘并通过 `Pack Doctor` 体检后，前端无需刷新页面即可实时热渲染生效。 |

---

## 2. `packs/<pack_id>/theme.json` 完整定制协议

`theme.json` 包含五大模块：`css_vars`（配色）、`typography`（字体排版）、`dialogue_box`（对话框样式）、`pixel_frame`（对话框周围留白区像素绘画）与 `skyline`（顶栏迷你天际线色板）。

### 2.1 配色 (`css_vars`) 与字体排版 (`typography`)

```json
{
  "id": "cyberpunk",
  "name": "赛博霓虹夜城",
  "css_vars": {
    "--bg-main": "#080a10",
    "--bg-panel": "#10141f",
    "--bg-card": "#161c2c",
    "--bg-input": "#0c0f18",
    "--text-main": "#e4ecf7",
    "--text-muted": "#8a9bb5",
    "--accent-primary": "#00f0ff",
    "--accent-secondary": "#ff2a85",
    "--accent-warn": "#ffb800",
    "--accent-danger": "#ff4d4f",
    "--border-color": "#24304a",
    "--radius-card": "6px",
    "--challenge-bg": "rgba(255, 42, 133, 0.13)",
    "--challenge-border": "#ff2a85",
    "--dialogue-color": "#7ee7ff"
  },
  "typography": {
    "font_body": "'Inter', 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif",
    "font_heading": "'Orbitron', 'Rajdhani', 'PingFang SC', sans-serif",
    "font_dialogue": "'PingFang SC', 'Microsoft YaHei', sans-serif",
    "font_mono": "'JetBrains Mono', 'Cascadia Code', Consolas, monospace",
    "story_font_size": "0.98rem",
    "story_line_height": "1.88",
    "letter_spacing": "0.01em"
  }
}
```

- **字体选型指南**：
  - 科幻 / 赛博 / 现代都市：干净利落的无衬线体（`Inter` / `PingFang SC`）配等宽终端数字字体；
  - 古典奇幻 / 历史权谋 / 东方志怪 / 维多利亚诡秘：温润厚重的衬线体（`'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', Georgia, serif`），行高可设为 `"1.95"`。

---

### 2.2 故事对话框样式协议 (`dialogue_box`)

主阅读区采用**居中对话框 + 四周留空画布**设计。`dialogue_box` 控制对话框本身的造型与四周留白宽度：

```json
"dialogue_box": {
  "style": "cyber-chamfer",
  "max_width": "740px",
  "gutter_x": "108px",
  "card_bg": "rgba(17, 22, 35, 0.90)",
  "border_width": "1px",
  "border_glow": "0 0 24px rgba(0, 240, 255, 0.14)",
  "corner_ornament": "◢",
  "quote_style": "bracket-glow",
  "action_box_style": "cyber-tag"
}
```

- **`style` 内置六大对话框造型骨架**（AI 任选其一并通过配色/光晕微调）：
  1. `"cyber-chamfer"`：赛博斜切角装甲框 + 霓虹双色边线微光；
  2. `"classic-rpg"`：经典双层像素高光内嵌边框（致敬复古 JRPG / CRPG 对话窗）；
  3. `"parchment-scroll"`：古卷书页质感边框 + 柔和内阴影与古典暗纹分割线；
  4. `"gothic-ornate"`：哥特悬疑暗金双线角花框 + 深邃阴影；
  5. `"terminal-crt"`：复古荧光终端扫描线细框 + 等宽抬头栏；
  6. `"minimal-glass"`：现代沉浸半透明磨砂极简卡片框。
- **`gutter_x`**：对话框左右两侧预留给像素画展示的最小留白宽度（建议 `"88px"` 至 `"132px"`）。
- **`quote_style`**：正文中角色台词 `"……"` 的着色方式（`"highlight"` 纯色高亮 | `"bracket-glow"` 微光底纹 | `"underline-soft"` 柔和底线）。

---

### 2.3 对话框四周留白区域的像素风格绘画协议 (`pixel_frame`) ★核心亮点

对话框外围的左、右、上、下留白区域挂载了一块原生整数倍缩放的像素画布（`image-rendering: pixelated`）。AI 可通过 `pixel_frame` 同时配置**「留白背景像素场景 + 动态微天气 + AI 亲手绘制的字符矩阵像素画精灵（`sprites`）」**：

```json
"pixel_frame": {
  "enabled": true,
  "pixel_scale": 4,
  "scene": "cyber_alley",
  "weather": "neon_rain",
  "bg_top": "#06080e",
  "bg_bottom": "#111628",
  "horizon_color": "#1a2238",
  "palette": {
    ".": null,
    "k": "#0b0f1a",
    "d": "#1b253b",
    "b": "#2c3d5e",
    "c": "#00f0ff",
    "m": "#ff2a85",
    "y": "#ffd23c",
    "w": "#e2e8f0",
    "g": "#10b981"
  },
  "sprites": [
    {
      "name": "neon_sign",
      "anchor": "left-top",
      "offset_x": 0,
      "offset_y": 16,
      "scale": 1,
      "glow": "rgba(255, 42, 133, 0.35)",
      "rows": [
        "..mmmmmmmmmm..",
        ".mkkkkkkkkkkm.",
        ".mkccckkccckm.",
        ".mkc.ckkc.ckm.",
        ".mkccckkccckm.",
        ".mkkkkkkkkkkm.",
        ".mkmmmkkyyykm.",
        ".mkkkkkkkkkkm.",
        "..mmmmmmmmmm..",
        ".....dddd.....",
        ".....d..d.....",
        "....dd..dd...."
      ]
    },
    {
      "name": "recon_drone",
      "anchor": "right-top",
      "offset_x": 0,
      "offset_y": 24,
      "scale": 1,
      "glow": "rgba(0, 240, 255, 0.35)",
      "rows": [
        "cc..........cc",
        ".cc..dddd..cc.",
        "...ddbbbbdd...",
        "..dbbwccwbbd..",
        "..dbbccmmbbd..",
        "...ddbbbbdd...",
        "....d.cc.d....",
        "...d......d..."
      ]
    }
  ]
}
```

#### `pixel_frame` 创作规范与技巧（供 AI 自由挥洒创意）
1. **`scene`（四周留白背景像素底景）**：
   - `"cyber_alley"`：左右留白区耸立多层次像素摩天楼、发光像素窗格与架空线缆；
   - `"misty_mountains"`：左右留白区延展像素远山峭壁、苍松飞檐与水波倒影；
   - `"gothic_cathedral"`：左右留白区立有哥特尖拱剪影、煤气街灯、残月与铁艺栏杆；
   - `"rust_wasteland"`：左右留白区散布断桥残骸、工业烟囱与荒原风滚草剪影；
   - `"star_sector"`：深空星云像素团簇、空间站桁架边框与带环行星；
   - `"custom_canvas"`：不画预设地景，100% 由 AI 的 `sprites` 像素矩阵自由构图。
2. **`weather`（克制的低帧率复古像素微动态）**：
   - 可选 `"neon_rain"`（霓虹细雨）、`"embers"`（飘散火星余烬）、`"snow"`（静谧落雪）、`"fireflies"`（幽林萤火）、`"starlight"`（闪烁星尘）、`"fog"`（流动薄雾）或 `"none"`。
3. **`palette` + `sprites`（AI 手绘像素艺术矩阵）**：
   - 在 `palette` 中用单字符定义调色板（`"."` 固定代表透明像素，其余字符填 `#RRGGBB` 十六进制色值，建议配出高光、主色、暗部阴影三层明暗以增强立体像素感！）；
   - 在 `sprites` 中创作 **3～6 个贴合当前世界观标志物的像素画**（例如：诡秘题材画**黄铜提灯、秘仪之眼古籍、怀表齿轮、乌鸦**；奇幻题材画**纹章盾牌、燃烧火炬、药水瓶、宝箱**；废土题材画**辐射警示牌、拼装机甲头颅、篝火油桶**；星际题材画**跃迁信标、护卫舰、星球仪**）；
   - 每个 `sprite` 建议尺寸为宽 `10~20` 格、高 `10~24` 格，**请严格保持同一 `sprite` 内每一行 `rows` 字符串等宽**（引擎在落盘前也会执行右侧 `.` 透明格自动补齐归一化）；
   - **`anchor` 合法锚点位置**：
     - 对话框左侧留白区：`"left-top"`, `"left-mid"`, `"left-bottom"`
     - 对话框右侧留白区：`"right-top"`, `"right-mid"`, `"right-bottom"`
     - 紧贴对话框四角挂饰：`"box-corner-tl"`, `"box-corner-tr"`, `"box-corner-bl"`, `"box-corner-br"`

---

### 2.4 剧情驱动的动态主题与像素氛围切换协议 (`mood_presets`) ★沉浸联动

引导玩家定制世界 UI（尤其是开局向导阶段 ④ 或玩家要求做动态氛围）时，**不要只做一套死板的静态皮肤**，应当同步在 `theme.json` 中预设 **4 套贴合该世界观的情境动态氛围 (`mood_presets`)**，使游戏过程中随着剧情转入交火危机、潜行调查或安全屋休整，界面主强调色、对话框呼吸光晕与四周留白区的像素微天气能够**随剧情自动丝滑切换**！

```json
"mood_presets": {
  "default": {
    "label": "🌧️ 常态·霓虹夜雨",
    "weather": "neon_rain",
    "bg_top": "#06080e",
    "bg_bottom": "#111628",
    "border_glow": "0 0 24px rgba(0, 240, 255, 0.16)"
  },
  "combat": {
    "label": "🔥 冲突·高危交火",
    "weather": "embers",
    "bg_top": "#16070d",
    "bg_bottom": "#2a0c18",
    "border_glow": "0 0 28px rgba(255, 42, 133, 0.32)",
    "css_vars": {
      "--accent-primary": "#ff3b6b",
      "--border-color": "#4f1d32",
      "--dialogue-color": "#ff9eb5"
    }
  },
  "mystery": {
    "label": "🌫️ 悬疑·禁区潜行",
    "weather": "fog",
    "bg_top": "#050914",
    "bg_bottom": "#0e1c30",
    "border_glow": "0 0 24px rgba(124, 92, 255, 0.24)",
    "css_vars": {
      "--accent-primary": "#8b5cf6",
      "--border-color": "#2b2554",
      "--dialogue-color": "#c4b5fd"
    }
  },
  "rest": {
    "label": "☕ 休整·安全屋微光",
    "weather": "fireflies",
    "bg_top": "#0d0b0a",
    "bg_bottom": "#1d1612",
    "border_glow": "0 0 22px rgba(245, 158, 11, 0.18)",
    "css_vars": {
      "--accent-primary": "#f59e0b",
      "--border-color": "#3f2e1e",
      "--dialogue-color": "#fde68a"
    }
  }
}
```

- **标准情境键名建议**：优先包含 `"default"`（日常探索）、`"combat"`（战斗/生死危机）、`"mystery"`（潜行/解密/诡秘异象）、`"rest"`（休整/酒馆/温情日常），也可根据玩家偏好增加题材特有情境（如 `"storm"` 辐射尘暴、`"sanctuary"` 圣所）。
- **运行时自动联动机制**：
  1. 台前 GM 在每回合回写 `world/now.md` 时，只需根据当前场景气氛设置 `ui_mood: combat`（或 `default` / `mystery` / `rest`），前端收到回合结果后即刻平滑过渡配色、对话框辉光与四周像素天气动画；
  2. 玩家也可在开局向导阶段 ④ 或顶部世界横幅的 **「🎭 氛围预设」** 胶囊上随时点选预览任一情境效果，或切回「自动跟随剧情」。

---

## 3. 右侧固定状态栏的数据映射 (`pack.yaml` → `ui_schema`)

右侧状态栏的整体布局与位置保持固定，AI 可在 `pack.yaml` 的 `ui_schema` 中配置该世界需要追踪的状态数据项：
- `world_status` / `player_status`：支持 `badge`、`bar`（进度条）、`fraction`（双数值如 `SAN 65/80`）、`meter`（阈值警戒条）、`tags`（状态词条）、`kv`（键值行）六种自适应组件；
- `character_sheet.sections`：通过 `visible: false` 或 `hide_empty: true` 隐藏当前题材不存在的空字段；
- `journal_sections`：对齐 `player/journal.md` 的主观手札二级标题分栏。

---

## 4. 强制体检要求

每次生成或修改 `theme.json` 与 `pack.yaml` 后，必须运行世界包深度体检工具验证色彩格式、像素精灵矩阵合法性与 Schema 自洽性：

```bash
python engine/tools/pack_io.py doctor <pack_id>
```
