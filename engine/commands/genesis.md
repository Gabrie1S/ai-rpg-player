---
description: 启动四阶段开局引导工作流（①自检并配置驱动 → ②配置世界 → ③孕育世界种子 → ④定制世界UI）
argument-hint: [一句话世界构想，如：蒸汽朋克克苏鲁调查 / 废土机甲佣兵]
---

玩家启动了四阶段开局引导：$ARGUMENTS

请加载并严格遵循 [`skills/genesis-onboarding/SKILL.md`](skills/genesis-onboarding/SKILL.md) 依次引导并执行：
1. **阶段 ①（系统自检与驱动）**：检查环境，确认当前 API / CLI / Harness 可用。
2. **阶段 ②（配置世界）**：使用已有世界包、导入玩家的世界包，或结合 [`skills/worldbuilder/SKILL.md`](skills/worldbuilder/SKILL.md) 生成 `packs/<pack_id>/`，并通过 `pack_io.py doctor` 体检。
3. **阶段 ③（孕育世界种子）**：运行 `newsave.py <世界名> --pack <pack_id>` 新建存档，调用 `world-sim`（`call_world_sim`、`world-sim` 子代理或 `python engine/tools/world_sim.py --save <世界名> --task "开局种子生成" --range "第0天" --request "..."`）静默生成第 0 天的势力、地图、人物与隐藏阴谋，并通过 `validate.py` 校验。
4. **阶段 ④（定制世界 UI）**：结合 [`skills/ui-designer/SKILL.md`](skills/ui-designer/SKILL.md)，保持宏观布局不变，生成配色、字体、对话框样式、四周像素画与 `mood_presets`，并通过体检。
