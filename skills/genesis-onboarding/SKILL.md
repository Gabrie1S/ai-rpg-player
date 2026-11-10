---
name: genesis-onboarding
description: 初次启动系统自检与四阶段开局引导工作流技能。依次完成：①系统自检与配置验证 API/CLI/Harness → ②配置世界（直接拖入 Pack 或引导生成世界规则包）→ ③孕育第 0 天世界实体种子 → ④生成定制配色/字体/对话框样式与周围留白像素绘画。
---

# 初次启动系统自检与四阶段开局引导工作流 (Genesis Onboarding Skill)

本技能定义了玩家初次启动引擎时的**零踩坑四阶段引导流水线**。每一步均有经过实战打磨的专属技能兜底校验，确保任何新玩家都能顺畅完成「驱动连通 → 世界就绪 → 定制专属像素画对话框 UI → 开启第一幕」。

---

## 0. 启动系统自检（Startup System Self-Check · `/api/system/selfcheck`）

每次初次启动时，引擎首先自动运行无感系统自检：
1. **运行时与工具链检查**：Python 版本与 UTF-8 编码、Git 快照回滚可用性、8 大防踩坑技能文件与三端入口文件（`AGENTS.md` / `CLAUDE.md` / `GEMINI.md`）完整性。
2. **主流驱动自动发现**：
   - **API 环境探测**：自动检测环境变量与本地免密推理端（如本机 `127.0.0.1:11434` Ollama 服务）；
   - **本地 CLI 自动发现**：按需扫描系统 `PATH` 中已安装的 Agent CLI（`claude` Claude Code、`codex` OpenAI Codex、`gemini` Gemini CLI、`agy` Antigravity CLI、`opencode` OpenCode）；
   - **Agent Harness 探测**：检测已安装的编程 Harness SDK（如 `google-antigravity` Python SDK、`claude-agent-sdk`）及外部 IDE 原生工作区状态。
3. **世界包与存档健康扫描**：扫描 `packs/`（识别 `ready` 标准包、`nested` 嵌套待修复目录、`unparsed` 散装设定稿）与 `saves/` 当前状态，智能定位玩家应进入的引导阶段。

---

## 阶段 ①：引导配置 API / CLI / Harness 并执行实测检查（Driver Setup & Live Check）

若检测到尚未完成驱动验证，首先引导玩家选择最顺手的接入方式并点击**「🔌 测试连接并保存」**（调用 `/api/driver/verify`）：

1. **模式 A：Direct API 模式（主流大模型接口全覆盖）**
   - 内置一键预设：DeepSeek、OpenAI、**Anthropic Claude 原生 `/v1/messages` 接口**、Google Gemini、OpenRouter、SiliconFlow 及本地 Ollama / LM Studio。
   - **连接后实测**：发送极轻量级探针请求，实测网络延迟（ms）、鉴权有效性与协议兼容性，通过后写入 `driver_verified: true`。
2. **模式 B：本地 CLI 桥接模式**
   - 支持一键选用本机已登录的 `claude`、`codex`、`gemini`、`agy`、`opencode`。
   - **连接后实测**：校验可执行文件路径与 `--version` 退出状态，再通过实际非交互入口发送简短探针，收到预期回复才算通过。
3. **模式 C：Agent Harness 模式**
   - OpenAI Agents SDK 和 Claude Agent SDK 通过实际模型探针后可用于网页驱动。Antigravity SDK 在接通引擎工具前暂不开放网页执行。外部 IDE / 终端协同仅检查仓库文件，玩家在 IDE 内对话主持，Web 端显示落盘结果。

4. **幕后模型选择（推荐配更强的模型）**
   - 台前写剧情要快，幕后推演世界、设计阴谋要想得深，所以推荐幕后用更强的模型。玩家选择后写入 `config.yaml` 的 `back_driver`，世界包和存档里都不记录。
   - **Direct API**：默认填入同厂商的推荐模型，可改；也可以让幕后用另一家的接口。幕后模型也会实测，失败时台前配置照常保存，提示玩家修改或换回同一个模型。
   - **Claude Code CLI**：幕后子代理通过 `CLAUDE_CODE_SUBAGENT_MODEL` 使用所选模型（在终端直连时也可写入 `config.local.yaml` 的 `back_driver.model` 或本地环境变量）。
   - **OpenAI Agents SDK**：同样支持在 `back_driver` 中独立指定更强的幕后推理模型；其他本地 CLI 的幕后默认与台前共用同一模型（或通过 `world_sim.py` 读取 `back_driver`）。

> **防踩坑门禁**：网页向导依据当前配置的实测记录判断就绪。连接探针只验证响应能力，世界生成和界面定制另行校验落盘结果。

---

## 阶段 ②：引导配置世界（二选一：直接拖入 Pack vs 走世界生成流程）

驱动就绪后，引导玩家配置世界规则底座（由 [`pack-porter`](../pack-porter/SKILL.md) 与 [`worldbuilder`](../worldbuilder/SKILL.md) 双技能支撑）：

- **路径 A：直接拖入现成 Pack 或设定稿（Drag & Drop 即拖即检）**
  - 玩家可直接将 `.rpgpack`（规则包或第 0 天母本种子）、`.zip` 压缩包或散装设定文件夹拖入引导窗口（或放入 `packs/` 目录）。
  - 系统自动执行**嵌套目录扁平化 + Pack Doctor 深度体检**：
    - 若拖入的是**脱敏世界母本种子（`.rpgpack`）**：直接解包创建就绪存档，一键跳至**阶段 ④ 定制 UI**或直接开局！
    - 若拖入的是**标准世界规则包（`ready`）**：体检通过后自动挂载激活，进入**阶段 ③ 孕育世界种子**。
    - 若拖入的是**散装设定文档（`unparsed`）**：自动读取全部原稿并交由路径 B 的 AI 编译补全为标准包。
- **路径 B：从零引导生成世界规则包（`worldbuilder` 防踩坑工作流）**
  - 玩家点选灵感预设或用几句话写下世界构想 → AI 一次性生成 `packs/<pack_id>/` 下的 `pack.yaml`、`setting.md`、`rules.md`、`rules.yaml`、`protagonist.md`、`style_pack.md`，并自动通过 `Pack Doctor` 0 错误校验。

---

## 按世界包完整度调整引导

向导不是固定走完四步：世界包的 `setup` 清单决定问什么，见 [`skills/pack-designer/SKILL.md`](../pack-designer/SKILL.md)。完整的包可能只问一个名字，第 ③ 步完成后直接写序章；留白多的包逐项询问；没有声明 `setup` 的包用自由填写的开局偏好。

---

## 长任务与中断

生成世界可能要很久。同一时间只跑一个长任务，页面顶部会显示已用时间和进度。关掉页面任务继续；关掉启动窗口任务中断，存档里留下 `world/seed_status.yaml` 标记，下次打开会提示继续生成，已有内容不会丢。

用已有世界的名字再生成一次时，新内容写进隐藏的候选存档（带 `world/candidate.yaml` 标记），旧世界不动，取消或失败也没有损失。生成完由玩家选：用新的（旧世界改名留作备份）、保留旧的（丢弃新内容）、都留下（新世界另存一局）。名字属于另一个世界包时直接拒绝，请玩家换名字。

---

## 阶段 ③：启用世界生成的种子（World Seed Generation → `saves/<world_name>/`）

**调用技能支撑**：[`engine/agents/world-sim.md`](../../engine/agents/world-sim.md) + [`skills/gm-workflow/SKILL.md`](../gm-workflow/SKILL.md)

基于已就绪的世界规则包，孕育有血有肉、暗流涌动的第 0 天世界实体母本：
1. 调用 `newsave.py` 初始化独立 Git 存档仓库，将玩家亲定设定写入 `world/canon.md` 锁定；
2. 唤醒独立上下文的 `world-sim` **静默生成**（全程只显示无剧透演化状态，绝不向玩家暴露幕后黑幕）：
   - `world/secrets/` 三层阴谋总纲与分卷揭示计划；
   - 核心对立势力卡（带推进时钟 `clock`）、双向连通地图（开局点 `detail: full` + 邻近轮廓点 `outline`）、关键 NPC 卡（A 级人物必含秘密、冲突与 `if_ignored` 节点剧情线）；
   - 严格符合 `rules.yaml` 数值区间的 `player/sheet.md`、`player/journal.md`、`timeline.yaml`（≥ 3 条未来计划事件）、`hooks.md`、`director.md` 与 `stage.md`（必读文件 ≤ 6 个）。
3. 自动运行 `validate.py` 强校验并在 `exports/` 下备份一份脱敏母本种子 `<world_name>-seed.rpgpack`，无缝进入阶段 ④。

---

## 阶段 ④：引导创建定制化世界 UI、四周像素绘画与动态氛围预设（Custom UI & Dynamic Mood Presets）

**调用技能支撑**：[`skills/ui-designer/SKILL.md`](../ui-designer/SKILL.md)

世界与主角生成完毕后，引导玩家为该世界打造专属视觉体验（同时预设日常与特殊剧情下的动态变装方案）：
1. **保持总体宏观 Layout 恒定不变**：顶部系统栏、顶部世界信息栏、右侧状态栏与最右侧场外调教窗的位置和骨架结构完全固定。
2. **引导玩家确认或一键交由 AI 定制五大视觉层并写入 `packs/<pack_id>/theme.json`（及 `pack.yaml` 状态映射）**：
   - **全局主题配色（`css_vars`）**与顶栏天际线色板（`skyline`）；
   - **字体与排版质感（`typography`）**：正文字体、标题字体、台词字体、字号与行高；
   - **故事对话框样式（`dialogue_box`）**：切角霓虹框 / 古卷羊皮纸框 / 经典双线像素 RPG 框 / 哥特暗金框 / CRT 终端框 / 极简磨砂框、四角铭文装饰、光晕与台词高亮样式；
   - **对话框四周留白区域的像素风格绘画（`pixel_frame`）**：在居中对话框左右与上下的留空画布上，绘制契合世界观的像素背景剪影、像素微天气以及 **AI 亲手用字符矩阵绘制的专属像素艺术精灵（`sprites`）**；
   - **随剧情动态切换的主题氛围预设（`mood_presets`）**：引导并一次性生成 `default`（日常探索）、`combat`（高危冲突/火星余烬）、`mystery`（潜行诡秘/暗雾星尘）、`rest`（安全屋休整/暖光萤火）等多情境预设——玩家可在预览栏点击试看切换效果，进入游戏后将随 `world/now.md` 的 `ui_mood` 自动跟随剧情氛围变色换景！
3. 通过 `Pack Doctor` 校验后前端瞬间热加载生效，玩家即可一键点击「🎬 立即进入世界并写出序章第一幕」沉浸开玩！
