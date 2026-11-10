---
description: 三模式导出与外部设定集/酒馆世界书导入工坊
argument-hint: [pack | seed | novel | import <路径>]
---

玩家调用导入/导出工坊：$ARGUMENTS

请参考 [`skills/pack-porter/SKILL.md`](skills/pack-porter/SKILL.md) 执行导入或导出：
- **导出纯净题材规则包**：`pack_io.py export-pack <pack_id>`
- **导出脱敏世界母本种子（第0天开局态，抹除个人游玩痕迹，不回显剧透）**：`pack_io.py export-seed`
- **导出跑团小说成品（Markdown / HTML）**：`pack_io.py export-novel --format html`
- **导入 `.rpgpack` 或转换 SillyTavern 世界书**：`pack_io.py import <路径>` 或 `convert-lorebook <json>`
