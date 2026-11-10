---
description: 推进游戏时间（训练、改装、赶路、休整、调查等），触发幕后世界推演
argument-hint: <天数> [这段时间做了什么]
---

推进时间：$ARGUMENTS

1. 确认玩家这段时间在做什么，未说明则先询问。
2. 推进时钟（调用 `clock_advance` 或 `clock.py advance <天数> --reason "<做了什么>"`）。
3. 按当前题材包 `rules.md` 结算玩家这段时间的收获或恢复，通过 `apply_changes` 或 `apply.py` 更新 `player/sheet.md`（若赶路则同步更新 `world/now.md` 的 `location`）。
4. 若提示有到期/前兆事件或达到推演间隔，调用幕后 `world-sim`（API 调 `call_world_sim`、Claude Code 调 `world-sim` 子代理、其他 CLI 运行 `python engine/tools/world_sim.py --task "推演" --range "<时间段>" --request "<事项>"`）推演对应时间段。
5. 根据简报撰写时间流逝正文：玩家的收获、归来后察觉到的环境变化与新传闻；玩家在场的事件直接在场景中演出。
