---
description: 撤销上一轮并重新推演（Swipe / Undo Last Turn）
argument-hint: [对重演的补充要求或行动修改]
---

玩家要求撤销上一轮并重新推演：$ARGUMENTS

1. 运行 `python engine/tools/timeline.py undo` 撤回最近一轮。它会回到该回合之前，并保留回合之后的场外修改（`[tuner]` `[gm]`）；与被撤回内容冲突的修改不会保留，并在输出中说明。不要直接用 `git reset`。
2. 重新读取回退后的 `S/world/now.md`，结合玩家的补充意见 `$ARGUMENTS` 重新执行该回合。
