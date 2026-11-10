---
description: 基于当前或指定的题材剧本包，新建一个世界存档并按游戏设计流程生成开局
argument-hint: [世界名] [--pack 题材包ID]
---

新建世界存档：$ARGUMENTS（若当前已加载新存档骨架，直接基于当前存档开局；若在终端尚未建档且未指定名字则先问玩家）。

1. **确认题材包与主角待定项**：
   - 题材包 `P = packs/<题材包ID>/`（默认读取 `config.yaml` 的 `active_pack`；若玩家拖入了尚未解析的设定文件夹，先按 `skills/pack-porter/SKILL.md` 将其解析补全为标准包）
   - 已知设定直接采用 `P/setting.md`、`P/rules.md` 与 `P/protagonist.md`（亲定事实不重复问）
   - 若在终端新建且缺必填项，只问存档名及 `pack_setup.py show <题材包ID>` 列出的项，并用 `pack_setup.py apply` 写入回答
2. **建存档（若尚未建档）**：`python engine/tools/newsave.py <世界名> --pack <题材包ID>`
3. **幕后分步创世（若当前存档尚未生成第 0 天种子）**：调用 `world-sim`（`call_world_sim`、`world-sim` 子代理或 `python engine/tools/world_sim.py --task "开局种子生成" --range "第0天" --request "..."`）执行开局生成（`canon.md` → `lore.md` + `design/` + `secrets/` → 势力与地图 → A/B级 NPC 与剧情线 → 序章与未来时间线 → `hooks.md` / `director.md` / `stage.md`）。生成内容绝不向玩家剧透。
4. **初始化主角与位置（若尚未初始化）**：按 `P/protagonist.md` 填写 `player/sheet.md` 与 `player/journal.md`（仅写主角已知的日常），设置 `world/now.md` 的初始 `location` 与 `present`。
5. **开场正文**：按 `skills/narrative-craft/SKILL.md` 与 `P/style_pack.md` 写出序章第一幕，停在需要玩家行动的地方（引擎会自动校验并提交开局回合快照）。
