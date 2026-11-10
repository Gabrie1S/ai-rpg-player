---
description: 结束本局：撰写本局记录、校验数据并持久化 Git 存档
---

1. 撰写本局记录 `log/sessions/NNN.md`（先查看 `log/sessions` 目录确定下一个顺延编号，如 `001.md`）：日期范围、发生的事、玩家的重要选择、悬而未决的线索（**主角视角，不含任何幕后秘密**），通过当前环境的原子写入工具（`apply_changes` 的 `add_file` 或 `apply.py`）写入。
2. 章节摘要（`log/summaries.md`）由引擎每 12 轮自动写，这里不用再写；每轮的原文和状态也已自动保存，`/save` 只是额外留一份本局记录。
3. 检查 [`skills/gm-workflow/SKILL.md`](skills/gm-workflow/SKILL.md) 中的「重要时刻」与「回写清单」是否有遗漏，若有遗漏一并补写。
4. 引擎会在本指令结束后自动执行合规校验并提交 `[save]` Git 存档快照。用简短自然的话告知玩家已妥善存档，下次开局可使用 `/recap`。
