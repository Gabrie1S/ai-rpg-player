---
description: 交互式创世工坊：引导设定全新世界观、等级属性体系、状态栏与日志卡结构、以及专属Web主题
argument-hint: [新题材包ID，如 steampunk / occult / wasteland]
---

玩家希望创建一个全新的题材剧本包：$ARGUMENTS

请加载并严格遵循 [`skills/worldbuilder/SKILL.md`](skills/worldbuilder/SKILL.md) 的六步交互式创世流程，文件结构遵循 [`skills/pack-designer/SKILL.md`](skills/pack-designer/SKILL.md)：
1. 定世界基调与核心冲突 → 生成 `packs/<id>/setting.md`
2. 定阶位、适性、潜能、唯一主引擎与属性 Schema 映射 → 生成 `packs/<id>/rules.md` 与 `packs/<id>/rules.yaml`
3. 定主角起手模板与可选特殊能力（如是否挂载 `skills/causal-foresight`） → 生成 `packs/<id>/protagonist.md`
4. 定制世界状态栏与玩家状态栏字段（`ui_schema.world_status` & `ui_schema.player_status`）
5. 定制角色日志卡分栏结构（`ui_schema.journal_sections`）
6. 生成世界专属文风手册与 Web 主题（基于默认模板生成 `packs/<id>/theme.json`、`style_pack.md`、`pack.yaml`）
7. 运行 `pack_io.py doctor <id>` 验证通过。
