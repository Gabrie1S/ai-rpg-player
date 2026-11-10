---
description: 上帝视角：跳出游戏，以最高权限查看真相、修改世界事实、回档、锁定或调教文风与规则
argument-hint: <指令，如：看看某NPC的真实想法 / 把这段重写得干脆点 / 锁定当前街区设定>
---

玩家跳出角色扮演，以上帝视角下达指令：$ARGUMENTS

上帝视角拥有最高权限。使用游戏外直接、简短的语气回应：

- **查看真相**：玩家明确要求查看 NPC 秘密、时间线（`world/events/timeline.yaml`）、导演笔记或 `secrets/` 时，先提醒“这会剧透”，玩家二次确认后再展示。
- **改写世界事实**：通过当前环境的存档写入方式（`apply_changes` 工具或 `apply.py`，若目标带 `locked: true` 可传 `"force_locked": true`）修改指定事实，必要时调用 `world-sim`（`call_world_sim`、`world-sim` 子代理或 `python engine/tools/world_sim.py --task "改世界" --request "..."`）执行连锁检查，并在 `log/changelog.md` 记录一行。
- **回档 / 撤销**：撤回最近一轮用 `manage_timeline(action="undo")` 或 `python engine/tools/timeline.py undo`（可连续执行逐轮回退）；要查看最近快照记录用 `manage_timeline(action="log")` 或 `python engine/tools/timeline.py log -n 15`。
- **冻结 / 解冻世界**：修改 `world/now.md` 的 `frozen` 字段。
- **锁定 / 解锁实体**：修改目标文件 YAML 头的 `locked` 字段。
- **调教文风与规则**：当场按意见改写，并将新要求写入本存档 `mods/style_pack.md` 或当前题材包的 `style_pack.md`。
