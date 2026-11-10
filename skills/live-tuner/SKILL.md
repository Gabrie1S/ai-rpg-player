---
name: live-tuner
description: 双通道玩家反馈与按需场外调教技能。定义主输入框括号指令的当轮静默响应规则，以及右侧固定窗口按需唤醒 tuner-agent 的四大调教闭环（调配色/字体/对话框与像素画、调文风、补设定、润色正文）。
---

# 双通道玩家反馈与实时场外调教技能 (Live Tuner Skill)

本引擎提供**双通道玩家反馈机制**，让玩家既能在故事推进中顺手叮嘱一句，也能在右侧固定调教窗口随时唤醒独立上下文的 **场外调教助手 (`tuner-agent`)** 深度定制配色、对话框像素画、规则与文风。

---

## 1. 通道 A：主输入框内顺手反馈（零面板切换）

1. **显式场外微调指令**（如 `我推门走进酒馆（场外：注意酒保还不认识我，态度冷淡点）`、`（OOC：这段快进几天）`）：
   - `engine/tools/context.py` 会自动提取此类括号指令，高亮注入当轮置底 `Author's Note`。
   - **台前 GM 准则**：当轮静默照办并继续推进故事，**绝不在正文里写任何 OOC 回复或停下反问**。
2. **未标前缀的普通括号**：由台前 GM 自动区分是角色内心独白（写入心理，NPC 听不见）还是对主持人的顺带要求（照办继续推剧情）。

---

## 2. 通道 B：右侧固定调教窗口（按需单次唤醒 `tuner-agent`）

最右侧设有位置恒定的独立场外调教窗（`Tuner Dock`）：
- **严格按需触发**：平时零后台驻留、零 Token 消耗；仅在玩家点击发送时单次唤醒。
- **上下文完全隔离**：与左侧主线叙事完全独立，讨论再多轮配色、像素画或规则修改也绝不污染主线记忆。
- **回复不剧透**：玩家能看到调教助手说的每一句话。它可以读幕后文件来做事，但回复里只说玩家已经知道的内容，细则见 [`engine/agents/tuner-agent.md`](../../engine/agents/tuner-agent.md)。

### `tuner-agent` 自动执行闭环

有存档时，界面、文风、规则这类覆盖写进存档的 `mods/`，不改世界包；主角性格、人物、剧情走向这类对当前世界的调教直接改存档里的对应文件，见 [`mod-builder`](../mod-builder/SKILL.md) 的对照表；玩家还可以让它做全新的命令和面板，见 [`mod-builder`](../mod-builder/SKILL.md)。

1. **闭环一：定制配色、字体、对话框样式与四周像素绘画（联动 [`ui-designer`](../ui-designer/SKILL.md)）**
   - 保持顶部信息栏与右侧状态栏等总体宏观 Layout 固定不变；
   - 调用 `update_world_ui` 更新 `packs/<pack_id>/theme.json`（`css_vars` 配色、`typography` 字体、`dialogue_box` 对话框造型、`pixel_frame` 对话框周围留白区的像素场景与手绘 `sprites` 像素画矩阵）或 `pack.yaml` 的状态映射，自动通过 `Pack Doctor` 校验并触发前端无感热刷新。
2. **闭环二：微调文风与通用提示词（联动 [`narrative-craft`](../narrative-craft/SKILL.md)）**
   - 调用 `update_narrative_style` 更新 `config.yaml` 通用叙事提示词或向 `packs/<pack_id>/style_pack.md` 追加规则/正反例，并同步登记 `engine/docs/playtest.md`。
3. **闭环三：补充世界观或修正当前存档状态（联动 [`worldbuilder`](../worldbuilder/SKILL.md)）**
   - 调用 `update_world_or_save` 更新世界包文档或对当前存档执行原子 `save_ops` 操作（自动跑 `validate.py` 强校验，报错则原子回滚修正后再提交）。
4. **闭环四：局部润色最近一轮正文（不回滚游戏状态）**
   - 调用 `polish_latest_story` 原地替换最新一轮正文措辞并打 Git 快照，完整保留已结算的存档状态。
