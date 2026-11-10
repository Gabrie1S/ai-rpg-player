# AI-RPG-Player · Claude Code 兼容入口 (CLAUDE.md)

> **兼容说明**：Claude Code CLI（`claude`）在启动时默认自动寻址根目录下的 `CLAUDE.md`（不会默认读取 `AGENTS.md`）。本文件作为面向 Claude Code 的薄入口指针保留，完整规则统一收口于 [`AGENTS.md`](AGENTS.md)、`skills/`、`engine/agents/` 与 `engine/commands/`。

1. **首先阅读通用总纲**：[`AGENTS.md`](AGENTS.md)
2. **核心防踩坑技能索引**：
   - 启动自检与四步开局引导：[`skills/genesis-onboarding/SKILL.md`](skills/genesis-onboarding/SKILL.md)
   - 叙事工艺与首次出场/防谜语人铁律：[`skills/narrative-craft/SKILL.md`](skills/narrative-craft/SKILL.md)
   - 回合状态机、弹性时间与原子回写：[`skills/gm-workflow/SKILL.md`](skills/gm-workflow/SKILL.md)
   - 世界观创世与表里防剧透生成：[`skills/worldbuilder/SKILL.md`](skills/worldbuilder/SKILL.md)
   - 世界包文件规范：[`skills/pack-designer/SKILL.md`](skills/pack-designer/SKILL.md)
   - 存档定制层与自定义面板：[`skills/mod-builder/SKILL.md`](skills/mod-builder/SKILL.md)
   - 世界包热插拔与 Pack Doctor 体检：[`skills/pack-porter/SKILL.md`](skills/pack-porter/SKILL.md)
   - 固定宏观布局 + 配色/字体/对话框样式 + 四周像素画定制：[`skills/ui-designer/SKILL.md`](skills/ui-designer/SKILL.md)
   - 双通道反馈与按需场外调教：[`skills/live-tuner/SKILL.md`](skills/live-tuner/SKILL.md)
3. **中立子代理与斜杠命令（`.claude/` 仅保留薄兼容指针）**：
   - 幕后推演子代理：[`engine/agents/world-sim.md`](engine/agents/world-sim.md)（Claude 兼容指针：[`.claude/agents/world-sim.md`](.claude/agents/world-sim.md)）
   - 按需场外调教子代理：[`engine/agents/tuner-agent.md`](engine/agents/tuner-agent.md)（Claude 兼容指针：[`.claude/agents/tuner-agent.md`](.claude/agents/tuner-agent.md)）
   - 斜杠命令定义：`engine/commands/`（`/genesis`, `/newpack`, `/newworld`, `/recap`, `/tick`, `/gm`, `/save`, `/snapshot`, `/retry`, `/export`, `/foresight`）
