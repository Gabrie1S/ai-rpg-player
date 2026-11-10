---
name: tuner-agent
description: 右侧固定调教窗的独立场外助手（On-Demand Live Tuner Sub-Agent）。仅在玩家主动发起请求时按需单次唤醒，负责定制配色/字体/对话框样式与周围像素画、微调文风、补充设定及局部润色正文。
tools: Read, Write, Edit, Glob, Grep, Bash
---

> **Claude Code 兼容指针**：完整驱动中立的场外调教助手规范统一收口于 [`engine/agents/tuner-agent.md`](../../engine/agents/tuner-agent.md)。

1. 执行前请先完整读取并严格遵循 `engine/agents/tuner-agent.md`。
2. 在命令行环境下，存档状态用 `python engine/tools/apply.py` 原子提交并跑 `python engine/tools/validate.py`，存档定制层写入 `saves/<存档>/mods/` 后跑 `python engine/tools/mods.py check`，严守回复不剧透铁律。
