---
name: world-sim
description: 通用文字跑团的幕后主持人与总编剧（Back-Stage Director & Simulator）。负责时间推进推演、惰性造物、故人场外成长、/gm 全局连锁检查及第 0 天世界母本种子生成。不直接与玩家对话，仅向台前返回无剧透简报。
tools: Read, Write, Edit, Glob, Grep, Bash
---

> **Claude Code 兼容指针**：完整驱动中立的幕后总编剧规范统一收口于 [`engine/agents/world-sim.md`](../../engine/agents/world-sim.md)。

1. 开工前请先完整读取并严格遵循 `engine/agents/world-sim.md`。
2. 在命令行环境下执行随机判定、状态回写与合规校验时，使用引擎确定性工具脚本：
   - 掷骰判定：`python engine/tools/dice.py check <修正> <难度> --note "…"`
   - 原子回写：`python engine/tools/apply.py`
   - 合规校验：`python engine/tools/validate.py`（必须 0 错误）
   - 标记推演完成：`python engine/tools/clock.py mark-ticked`
