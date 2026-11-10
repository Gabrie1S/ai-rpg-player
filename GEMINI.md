# AI-RPG-Player · Gemini CLI 兼容入口 (GEMINI.md)

> **兼容说明**：Google Gemini CLI（`gemini`）在启动时默认自动读取根目录下的 `GEMINI.md`。本文件作为面向 Gemini CLI 的薄入口指针，完整主持规范统一收口于 [`AGENTS.md`](AGENTS.md)、`skills/`、`engine/agents/` 与 `engine/commands/`。

1. **首先阅读通用总纲**：[`AGENTS.md`](AGENTS.md)
2. **按任务阶段加载对应防踩坑技能与中立代理规范**：
   - 初次启动与四步引导：[`skills/genesis-onboarding/SKILL.md`](skills/genesis-onboarding/SKILL.md)
   - 台前叙事与防踩坑文风：[`skills/narrative-craft/SKILL.md`](skills/narrative-craft/SKILL.md)
   - 标准回合与原子落盘：[`skills/gm-workflow/SKILL.md`](skills/gm-workflow/SKILL.md)
   - 创世架构与防剧透种子：[`skills/worldbuilder/SKILL.md`](skills/worldbuilder/SKILL.md) & [`engine/agents/world-sim.md`](engine/agents/world-sim.md)
   - 世界包管理与体检：[`skills/pack-porter/SKILL.md`](skills/pack-porter/SKILL.md)
   - 定制 UI 与对话框周围像素画：[`skills/ui-designer/SKILL.md`](skills/ui-designer/SKILL.md)
   - 场外实时调教：[`skills/live-tuner/SKILL.md`](skills/live-tuner/SKILL.md) & [`engine/agents/tuner-agent.md`](engine/agents/tuner-agent.md)
   - 通用斜杠命令规范：`engine/commands/`
