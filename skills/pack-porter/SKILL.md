---
name: pack-porter
description: 世界观包热插拔加载、浏览器直拖导入、深度体检与三模式打包分享技能。支持直接拖入 .rpgpack/.zip/设定文件夹自动检验、嵌套目录一键扁平化、散装设定AI编译及三模式脱敏导出。
---

# 世界观包热插拔、直拖检验与导入导出 (Pack Porter Skill)

本技能负责管理 `packs/` 目录下的独立世界观包生态，实现**「直接拖入即可体检加载、删除文件夹即可干净卸载、一键打包即可脱敏分享」**的零耦合体验。

世界包的文件结构见 [`skills/pack-designer/SKILL.md`](../pack-designer/SKILL.md)。

---

## 1. 两种「直接拖入 Pack」热插拔路径

1. **Web 界面直接拖拽上传（Browser Drag & Drop）**：
   - 在初次启动向导**阶段 ②（配置世界）**或**世界包中心（Pack Hub）**中，玩家可直接将 `.rpgpack`（规则包或第 0 天世界母本种子）、`.zip` 压缩包或散装设定文档拖入网页虚线框；
   - 引擎自动完成解包、嵌套层级扁平化与 **Pack Doctor 深度体检**：
     - 若是 `.rpgpack` 世界母本种子 → 直接创建就绪存档，立即可玩；
     - 若是标准世界规则包（`status: "ready"`）→ 立即激活并引导孕育世界种子；
     - 若是散装 `.md` / `.txt` 设定稿（`status: "unparsed"`）→ 安全落入 `packs/<id>/` 并提供一键 AI 编译补全，绝不报错崩溃。
2. **本地文件系统直拖（`packs/<pack_id>/`）**：
   - 玩家也可随时把文件夹拖进项目 `packs/` 目录，点击「按需扫描」或运行 `pack_io.py list` 即刻识别；
   - 若解压多套了一层目录（如 `packs/foo/foo/pack.yaml`），自动标记为 `status: "nested"`，点击「一键扁平化修复」或运行 `pack_io.py flatten <pack_id>` 即可归位；
   - 删除 `packs/<pack_id>/` 即可卸载，引擎底层具备全链路回退兜底，删除当前正在游玩的包也不会导致主程序崩溃。

---

## 2. 检验、导入与三模式导出 CLI 命令（项目根目录执行）

```bash
# 1. 按需扫描 packs/ 下所有世界包状态 (ready / unparsed / nested)
python engine/tools/pack_io.py list

# 2. 深度体检指定世界包（校验核心文件、Schema 交叉自洽性、对话框样式与像素精灵矩阵）
python engine/tools/pack_io.py doctor <pack_id>
python engine/tools/pack_io.py inspect <pack_id> [--json]

# 3. 修复解压多套一层子目录的嵌套世界包
python engine/tools/pack_io.py flatten <pack_id>

# 4. 导入 .rpgpack（自动识别规则包或世界种子）、普通 .zip 或外部设定文件夹
python engine/tools/pack_io.py import <路径> [--name <指定ID或存档名>]

# 5. 三模式导出（纯净规则包 / 脱敏第0天世界母本种子 / 独立排版跑团小说）
python engine/tools/pack_io.py export-pack <pack_id> [--out exports/<pack_id>-pack.rpgpack]
python engine/tools/pack_io.py export-seed [--save <存档名>] [--out exports/<存档名>-seed.rpgpack]
python engine/tools/pack_io.py export-novel [--save <存档名>] [--format html|md] [--out <输出路径>]

# 6. 转换 SillyTavern 世界书 (World Info JSON) 为 Markdown 词条草案
python engine/tools/pack_io.py convert-lorebook <json路径> --out <输出草案目录>
```
