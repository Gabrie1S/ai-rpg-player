"""分角色 API Tool Calling 定义与安全边界执行沙箱（front / back / tuner / genesis）。"""
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from ai_log import child_env_with_trace, log_error, log_event  # noqa: E402
from apply import apply_operations  # noqa: E402
from common import (  # noqa: E402
    PACKS,
    ROOT,
    active_pack_id,
    load_config,
    pack_dir,
    read_text,
    safe_load_yaml,
    save_config,
)
import mods as mods_layer  # noqa: E402
from pack_io import doctor_pack, normalize_theme_data  # noqa: E402
import story_log  # noqa: E402
import timeline  # noqa: E402
from turn_protocol import OPS_DOC, OPS_ITEM_SCHEMA  # noqa: E402
from validate import validate  # noqa: E402


class EngineToolFault(RuntimeError):
    """同一个工具反复报同一个异常：多半是引擎自身的问题，让模型换写法重试只会白白耗时，直接中止这次调用。"""


class ToolRunGuard:
    """一次模型调用（含多步工具往返）期间的工具执行记录，与驱动无关：API 循环和 Harness 都用它。

    - 熔断：同一工具、同一异常类型、同一报错出现 REPEAT_LIMIT 次就抛 EngineToolFault。
    - 写回结果：记下 apply_changes 的成败。最后一次失败之后再没有成功过，说明想写的改动没进存档，
      调用方据此判定这次调用没有完成，而不是把模型的“已完成”当真。
    - engine_settles_tick：推进“已推演到第几天”由引擎在验收通过后做，模型调 mark_ticked 不再生效。
    """

    REPEAT_LIMIT = 2

    def __init__(self, engine_settles_tick=False):
        self.engine_settles_tick = engine_settles_tick
        self.fault_counts = {}
        self.writes_ok = 0
        self.writes_failed = 0
        self.unresolved_write_errors = []

    def record_fault(self, tool, exc):
        key = (tool, type(exc).__name__, str(exc))
        self.fault_counts[key] = self.fault_counts.get(key, 0) + 1
        return self.fault_counts[key]

    def record_write(self, ok, errors=None):
        if ok:
            self.writes_ok += 1
            self.unresolved_write_errors = []
        else:
            self.writes_failed += 1
            self.unresolved_write_errors = [str(e) for e in (errors or ["未知错误"])][:5]


FRONT_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "read_save_file",
            "description": "按分层切片与三级目录索引（world/index.md、world/design/目录.md、world/secrets/目录.md）按需精读单个实体卡或单个主题切片文件（如 world/npcs/npc_xxx.md、world/design/02-xxx.md、world/secrets/01-xxx.md 或超长设定包切片 pack_slice:setting/01-xxx.md）。传目录（如 log/sessions）可列出文件。已预装配文件严禁重复读取；读取的秘密与暗线仅用于保持伏笔一致，未到揭示时机绝不在正文剧透。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "相对当前存档根目录的路径（如 world/npcs/npc_old_hawk.md）或 pack_slice:<模块>/<切片文件>"}
                },
                "required": ["rel_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clock_advance",
            "description": "推进游戏时间（天数 > 0），返回到期事件、前兆事件及是否需要调用 world-sim 推演。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "推进的天数（正整数）"},
                    "reason": {"type": "string", "description": "这段时间主角在做什么"},
                },
                "required": ["days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_changes",
            "description": "一次原子提交一批存档改动（人物卡、角色卡、手记、当前场景、平生大事、新建文件等），任何一条出错整批回滚，改好重交。\n\n" + OPS_DOC,
            "parameters": {
                "type": "object",
                "properties": {
                    "ops": {
                        "type": "array",
                        "description": "操作数组，每项写法见工具说明（字段名是 op / file，不是别的）",
                        "items": OPS_ITEM_SCHEMA,
                    },
                    "force_locked": {
                        "type": "boolean",
                        "description": "可选：仅在 /gm 上帝视角明确要求改写带 locked: true 的锁定文件时传 true",
                    },
                },
                "required": ["ops"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_world_sim",
            "description": "调用独立上下文的幕后主持人 world-sim 执行时间线推演、惰性造物、连锁检查或世界快照，返回无剧透简报。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "任务类型：推演 | 造物 | 改世界 | 整理阶段清单 | 世界快照 | 开局生成"},
                    "day_range": {"type": "string", "description": "时间范围，如 第1天~第7天"},
                    "request": {"type": "string", "description": "玩家当前位置及具体需要 world-sim 完成的事项"},
                },
                "required": ["task_type", "request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_timeline",
            "description": "在 /gm 上帝视角下查看最近回合快照记录或撤回最近一轮（对应 timeline.py log / undo，自动保留场外定制）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["log", "undo"],
                        "description": "log 查看最近快照；undo 撤回最近一轮",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "action=log 时列出的最大条数（默认 15）",
                    },
                },
                "required": ["action"],
            },
        },
    },
]

# 台前一次写完一轮时只需要查资料；回写走正文后的变更块，推演走 request_sim
FRONT_INLINE_TOOLS_OPENAI = [FRONT_TOOLS_OPENAI[0]]

BACK_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "read_save_file",
            "description": "按需读取当前存档内的任意档案文件（含 world/secrets/、world/director.md、world/events/timeline.yaml 等幕后全知档案）。传目录（如 world/design）就列出里面的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "相对当前存档根目录的路径"}
                },
                "required": ["rel_path"],
            },
        },
    },
    FRONT_TOOLS_OPENAI[2],
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "幕后推演不确定事件或势力时钟时掷骰判定（2d6 + 修正 vs 难度）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mod": {"type": "integer", "description": "修正值"},
                    "dc": {"type": "string", "description": "难度名称（简单/普通/困难/极难/传说）或数值"},
                    "note": {"type": "string", "description": "判定事由"},
                },
                "required": ["mod", "dc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_save",
            "description": "校验当前存档所有 YAML 头与引用是否合规。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_ticked",
            "description": "幕后推演完成后，将 now.md 的 last_tick_day 更新为当前游戏日。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TUNER_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "read_tuner_target",
            "description": "读取当前世界包文件/目录（如 pack:、pack:pack.yaml、pack:theme.json、pack:style_pack.md、pack:setting.md、pack:rules.yaml）或当前存档文件/目录（如 save:world/npcs、save:player/sheet.md、save:world/now.md）或 mods / config / latest_story。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "目标标识，如 'pack:'、'pack:theme.json'、'save:world/npcs'、'save:player/sheet.md'、'mods'、'config' 或 'latest_story'",
                    }
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_world_ui",
            "description": "按 skills/ui-designer/SKILL.md 定制或修改当前世界或世界包的视觉主题 (theme.json：含 css_vars 配色、typography 字体、dialogue_box 对话框样式、pixel_frame 对话框周围留白像素绘画与手绘 sprites 矩阵、skyline 天际线) 与状态映射 (pack.yaml 的 ui_schema)。保持顶部信息栏与右侧状态栏等总体 Layout 固定不变。",
            "parameters": {
                "type": "object",
                "properties": {
                    "theme_patch": {
                        "type": "object",
                        "description": "可选：要合并写入 theme.json 的内容（可含 name, css_vars, typography, dialogue_box, pixel_frame[含 scene, weather, palette, sprites], mood_presets[随剧情动态切换的情境预设如 default/combat/mystery/rest], skyline）",
                    },
                    "ui_schema_patch": {
                        "type": "object",
                        "description": "可选：要合并更新到 pack.yaml -> ui_schema 的数据映射（可含 tagline, hud_tabs, world_status, player_status, character_sheet, journal_sections）",
                    },
                    "summary": {"type": "string", "description": "本次 UI 与像素画定制的一句话说明"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_narrative_style",
            "description": "修改叙事提示词模板或向 style_pack.md 追加文风规则/正反例（有存档时自动写入本局 mods/，不影响其他存档），并登记 playtest.md。",
            "parameters": {
                "type": "object",
                "properties": {
                    "new_authors_note_template": {
                        "type": "string",
                        "description": "可选：替换叙事提示词模板全文（不改模板则留空）",
                    },
                    "style_rule_to_append": {
                        "type": "string",
                        "description": "可选：追加到 style_pack.md 的具体文风要求或注意事项",
                    },
                    "note": {"type": "string", "description": "本次文风调教摘要（将记入 playtest.md）"},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_world_or_save",
            "description": "补充修改设定文档（setting.md / rules.md / protagonist.md）或对当前存档执行原子状态/实体回写操作（自动跑 validate.py 校验）。\n\n" + OPS_DOC,
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_doc_name": {
                        "type": "string",
                        "description": "可选：要更新的设定文件名（限 setting.md, rules.md, protagonist.md, style_pack.md）",
                    },
                    "pack_doc_append": {
                        "type": "string",
                        "description": "可选：追加到上述设定文档末尾的 Markdown 内容",
                    },
                    "save_ops": {
                        "type": "array",
                        "description": "可选：对当前存档执行的原子操作数组（set / append / replace / add_file / append_file / set_panel_data）",
                        "items": OPS_ITEM_SCHEMA,
                    },
                    "summary": {"type": "string", "description": "本次设定/存档调整说明"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_mods",
            "description": "写入本存档的定制层 mods/（做法见 skills/mod-builder）：自定义面板、命令、设定补充、面板数据。不影响世界包，校验不通过会自动恢复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "object",
                        "description": "相对 mods/ 的路径 -> 文件全文。允许 pack.yaml、theme.json、setting.md、rules.md、protagonist.md、style_pack.md、backstage.md、commands/<名字>.md、panels/<id>.html、data/<id>.json",
                    },
                    "panels": {
                        "type": "array",
                        "description": "要声明或更新的面板，每项 {id, label, icon, gm_note}。gm_note 告诉主持人什么时候更新面板数据，只用来显示静态内容时不写。",
                        "items": {"type": "object"},
                    },
                    "summary": {"type": "string", "description": "一句话说明做了什么"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_mod",
            "description": "移除本存档的一项定制，传相对 mods/ 的路径。删面板时连带它的声明和数据。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_engine_extension",
            "description": "玩家要的东西用命令、规则、主题和面板都做不到时，记录下来交给引擎开发者。记录后如实告诉玩家目前做不到，并说明你提供了什么替代做法。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "一句话说明需要的能力"},
                    "detail": {"type": "string", "description": "玩家想达到什么效果，现有能力卡在哪里"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "polish_latest_story",
            "description": "在不回滚本轮世界与角色状态的前提下，局部替换或重写最近一轮的故事正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_snippet": {"type": "string", "description": "可选：要精准替换的原片段（若替换整段最新故事可留空）"},
                    "new_text": {"type": "string", "description": "润色后的新片段或完整新正文"},
                },
                "required": ["new_text"],
            },
        },
    },
]

GENESIS_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "read_pack_raw_files",
            "description": "读取 packs/<pack_id>/ 下现有的原始设定文档内容（用于解析玩家拖入的散装设定文件夹）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string", "description": "世界包文件夹名"},
                },
                "required": ["pack_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_pack_bundle",
            "description": "在 packs/<pack_id>/ 下写入或更新标准世界包文件（pack.yaml, setting.md, rules.md, rules.yaml, protagonist.md, style_pack.md, theme.json），并立即运行 Pack Doctor 深度体检。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string", "description": "英文/数字/下划线组成的世界包目录 ID，如 steampunk_occult"},
                    "files": {
                        "type": "object",
                        "description": "文件名 -> 文件完整文本内容的字典（仅允许标准世界包文件）",
                    },
                },
                "required": ["pack_id", "files"],
            },
        },
    },
]

ALLOWED_PACK_WRITE_FILES = {
    "pack.yaml",
    "setting.md",
    "rules.md",
    "rules.yaml",
    "protagonist.md",
    "style_pack.md",
    "theme.json",
    "backstage.md",
}
ALLOWED_PACK_WRITE_DIRS = ("commands/", "templates/world/", "panels/")


def _safe_pack_relpath(fname):
    """世界包写入白名单：根目录标准文件，以及 commands/、templates/world/、panels/ 下的文本文件。"""
    rel = str(fname or "").replace("\\", "/").strip().lstrip("/")
    if rel in ALLOWED_PACK_WRITE_FILES:
        return rel
    parts = rel.split("/")
    if ".." in parts or not rel.startswith(ALLOWED_PACK_WRITE_DIRS):
        return None
    allowed_suffix = (".html",) if rel.startswith("panels/") else (".md", ".yaml", ".json")
    if not rel.endswith(allowed_suffix):
        return None
    return rel


ALL_KNOWN_TOOL_NAMES = {
    t["function"]["name"]
    for group in (FRONT_TOOLS_OPENAI, BACK_TOOLS_OPENAI, TUNER_TOOLS_OPENAI, GENESIS_TOOLS_OPENAI)
    for t in group
}

FRONT_FORBIDDEN_PREFIXES = ()
TUNER_FORBIDDEN_SAVE_PREFIXES = ("world/secrets/",)
BACKSTAGE_ONLY_PACK_PREFIXES = ("backstage.md", "templates/")


def _safe_pack_id(raw_id):
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", str(raw_id or "").strip()).strip("_")
    return cleaned or "custom_world"


def _writes_to_mods(sd, pid, tuner_context):
    """有存档时，调教助手的改动进存档 mods，不碰世界包；创世向导给包补界面时才写世界包。"""
    if not sd or tuner_context.get("write_scope") == "pack":
        return False
    return active_pack_id(sd) == pid


def _list_dir_entries(root_dir: Path, target_dir: Path) -> str:
    names = sorted(
        x.relative_to(root_dir).as_posix() + ("/" if x.is_dir() else "")
        for x in target_dir.iterdir()
        if not x.name.startswith(".")
    )
    return "目录里有：\n" + "\n".join(names[:300]) if names else "这个目录是空的。"


def execute_tool(sd, name, args, role="front", world_sim_callback=None, tuner_context=None, guard=None):
    """在安全边界内执行单个工具调用并返回字符串结果（所有入参、文件变动与耗时均写入 AI 诊断日志）。

    guard（ToolRunGuard）由调用方的工具循环传入：用来熔断反复出现的同一个异常、记录写回成败。
    """
    tuner_context = tuner_context if isinstance(tuner_context, dict) else {}
    applied_actions = tuner_context.setdefault("applied_actions", [])
    t0 = time.perf_counter()
    sub_env = child_env_with_trace()
    if sd:
        sub_env["RPG_SAVE"] = sd.name

    try:
        if name == "read_save_file":
            rel = (args.get("rel_path") or "").strip().lstrip("/\\").replace("\\", "/")
            if rel.startswith("pack_slice:"):
                sub_rel = rel.split(":", 1)[1].lstrip("/\\").replace("\\", "/")
                if role != "back" and (sub_rel == "backstage" or sub_rel.startswith("backstage/")):
                    log_event("tool.acl_deny", lvl="WARN", role=role, tool=name, rel_path=rel)
                    return "权限隔离：世界包幕后指南切片仅限 world-sim 读取。"
                pdir = pack_dir(sd=sd, required=False)
                if not pdir:
                    return "当前未挂载世界包文件夹。"
                slice_root = (pdir / ".slices").resolve()
                sp = (slice_root / sub_rel).resolve()
                if slice_root not in sp.parents or not sp.is_file():
                    return f"世界包切片不存在或路径越界：{rel}"
                content = read_text(sp)
                log_event("tool.exec", role=role, tool=name, rel_path=rel, chars=len(content), ms=round((time.perf_counter() - t0) * 1000, 1))
                return content
            if rel.startswith("packs/"):
                pdir = pack_dir(sd=sd, required=False)
                target = (ROOT / rel).resolve()
                if not pdir or pdir.resolve() not in target.parents or not target.is_file():
                    return f"世界包文件不存在或路径越界：{rel}"
                pack_rel = target.relative_to(pdir.resolve()).as_posix()
                if role != "back" and any(pack_rel == pfx.rstrip("/") or pack_rel.startswith(pfx) for pfx in BACKSTAGE_ONLY_PACK_PREFIXES):
                    log_event("tool.acl_deny", lvl="WARN", role=role, tool=name, rel_path=rel)
                    return "权限隔离：世界包幕后指南与母本模板仅限 world-sim 读取。"
                return read_text(target)
            if not sd:
                return "当前尚未加载存档。"
            if role == "front" and any(rel == pfx.rstrip("/") or rel.startswith(pfx) for pfx in FRONT_FORBIDDEN_PREFIXES):
                log_event("tool.acl_deny", lvl="WARN", role=role, tool=name, rel_path=rel)
                return "权限隔离：该文件仅限 world-sim 读取。"
            root_resolved = sd.resolve()
            p = (sd / rel).resolve() if rel else root_resolved
            if p.is_dir() and (p == root_resolved or root_resolved in p.parents):
                out_list = _list_dir_entries(root_resolved, p)
                log_event("tool.exec", role=role, tool=name, rel_path=rel, listed=out_list.count("\n"), ms=round((time.perf_counter() - t0) * 1000, 1))
                return out_list
            if root_resolved not in p.parents or not p.is_file():
                return f"文件不存在或路径越界：{rel}"
            content = read_text(p)
            log_event("tool.exec", role=role, tool=name, rel_path=rel, chars=len(content), ms=round((time.perf_counter() - t0) * 1000, 1))
            return content

        elif name == "clock_advance":
            days = int(args.get("days", 1))
            reason = str(args.get("reason", ""))
            r = subprocess.run(
                [sys.executable, str(TOOLS_DIR / "clock.py"), "--save", sd.name, "advance", str(days), "--reason", reason],
                capture_output=True, text=True, encoding="utf-8", env=sub_env,
            )
            out = (r.stdout + "\n" + r.stderr).strip()
            log_event(
                "tool.exec",
                lvl="WARN" if r.returncode != 0 else "INFO",
                role=role, tool=name, days=days, reason=reason, rc=r.returncode, output=out,
                ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            return out

        elif name == "apply_changes":
            ops = args.get("ops") or []
            force_locked = bool(args.get("force_locked", False))
            res = apply_operations(sd, ops, force_locked=force_locked, rollback_on_error=True, commit_checkpoint=False)
            log_event(
                "tool.exec",
                lvl="ERR" if res["rolled_back"] else "INFO",
                role=role, tool=name, ops_count=len(ops), rolled_back=res["rolled_back"],
                errors=res["errors"], ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            if guard is not None:
                guard.record_write(not res["rolled_back"], res["errors"])
            if res["rolled_back"]:
                return "回写失败（已回滚）：\n" + "\n".join(res["errors"])
            return f"已成功回写 {res['done']}/{res['total']} 处，校验通过。"

        elif name == "manage_timeline":
            if not sd:
                return "当前尚未加载存档。"
            action = str(args.get("action") or "log").strip().lower()
            if action == "undo":
                if story_log.count_turns(sd) == 0:
                    return "当前尚无已归档的故事回合可撤回。"
                undone = timeline.undo_last_turn(sd)
                notice = timeline.describe_undo(undone)
                log_event("tool.exec", role=role, tool=name, action="undo", via_git=undone.get("via_git"), ms=round((time.perf_counter() - t0) * 1000, 1))
                return "✓ 已撤回最近一轮。" + (f"（{notice}）" if notice else "")
            limit = max(1, min(int(args.get("limit") or 15), 50))
            r = timeline._git(sd, "log", f"-n{limit}", "--oneline")
            out = (r.stdout.strip() if r and r.returncode == 0 else "") or "（暂无 Git 快照记录，可读取 log/story/ 查看回合档案）"
            log_event("tool.exec", role=role, tool=name, action="log", limit=limit, ms=round((time.perf_counter() - t0) * 1000, 1))
            return out

        elif name == "roll_dice":
            mod = str(args.get("mod", 0))
            dc = str(args.get("dc", "普通"))
            note = str(args.get("note", ""))
            cmd_args = [sys.executable, str(TOOLS_DIR / "dice.py")]
            if sd:
                cmd_args += ["--save", sd.name]
            cmd_args += ["check", mod, dc, "--note", note]
            r = subprocess.run(
                cmd_args,
                capture_output=True, text=True, encoding="utf-8", env=sub_env,
            )
            out = r.stdout.strip()
            log_event("tool.exec", role=role, tool=name, mod=mod, dc=dc, note=note, output=out, ms=round((time.perf_counter() - t0) * 1000, 1))
            return out

        elif name == "validate_save":
            rep, _ = validate(sd)
            log_event(
                "tool.exec",
                lvl="WARN" if rep.errors else "INFO",
                role=role, tool=name, errors=rep.errors, warnings=rep.warnings,
                ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            if not rep.errors:
                return "✓ 校验通过，无错误。"
            return "校验发现错误：\n" + "\n".join(f"✗ {e}" for e in rep.errors)

        elif name == "mark_ticked":
            if guard is not None and guard.engine_settles_tick:
                log_event("tool.exec", role=role, tool=name, skipped="engine_settles_tick", ms=round((time.perf_counter() - t0) * 1000, 1))
                return "这次推演由引擎在验收通过后自动记录进度，不需要调用 mark_ticked。改完存档、校验通过后直接写简报即可。"
            r = subprocess.run(
                [sys.executable, str(TOOLS_DIR / "clock.py"), "--save", sd.name, "mark-ticked"],
                capture_output=True, text=True, encoding="utf-8", env=sub_env,
            )
            out = r.stdout.strip()
            log_event("tool.exec", role=role, tool=name, output=out, ms=round((time.perf_counter() - t0) * 1000, 1))
            return out

        elif name == "call_world_sim":
            log_event("world_sim.invoke", role=role, sim_args=args)
            if world_sim_callback:
                sim_res = world_sim_callback(args)
                log_event("world_sim.done", role=role, brief_chars=len(sim_res or ""), brief=sim_res, ms=round((time.perf_counter() - t0) * 1000, 1))
                return sim_res
            return "幕后推演未配置回调。"

        # ==================== 场外调教助手 (tuner-agent) 工具 ====================
        elif name == "read_tuner_target":
            target = (args.get("target") or "").strip()
            pid = tuner_context.get("pack_id") or active_pack_id(sd)
            pdir = pack_dir(pid, sd=sd, required=False)
            if target == "config":
                cfg = dict(load_config())
                if isinstance(cfg.get("front_driver"), dict):
                    cfg["front_driver"] = {**cfg["front_driver"], "api_key": "***"}
                if isinstance(cfg.get("back_driver"), dict):
                    cfg["back_driver"] = {**cfg["back_driver"], "api_key": "***"}
                return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
            if target == "latest_story":
                return tuner_context.get("latest_story") or "（当前暂无故事正文记录）"
            if target.startswith("pack:"):
                fname = target.split(":", 1)[1].strip().lstrip("/\\").replace("\\", "/")
                if not pdir:
                    return "当前无挂载的世界包文件夹。"
                if any(fname == pfx.rstrip("/") or fname.startswith(pfx) for pfx in BACKSTAGE_ONLY_PACK_PREFIXES):
                    log_event("tool.acl_deny", lvl="WARN", role=role, tool=name, target=target)
                    return "场外调教助手默认不直接展开世界包幕后指南与开局秘密模板，以免在回复中剧透。"
                p_root = pdir.resolve()
                fp = (pdir / fname).resolve() if fname else p_root
                if fp.is_dir() and (fp == p_root or p_root in fp.parents):
                    return _list_dir_entries(p_root, fp)
                if p_root not in fp.parents or not fp.is_file():
                    return f"世界包文件不存在：{fname}"
                return read_text(fp)
            if target == "mods":
                return json.dumps(mods_layer.list_mods(sd), ensure_ascii=False, indent=2) if sd else "当前尚未加载存档。"
            if target.startswith("save:"):
                if not sd:
                    return "当前尚未加载存档。"
                rel = target.split(":", 1)[1].strip().lstrip("/\\").replace("\\", "/")
                if any(rel == pfx.rstrip("/") or rel.startswith(pfx) for pfx in FRONT_FORBIDDEN_PREFIXES + TUNER_FORBIDDEN_SAVE_PREFIXES):
                    log_event("tool.acl_deny", lvl="WARN", role=role, tool=name, target=target)
                    return "场外调教助手默认不直接展开幕后机密全文，以免在回复中剧透。"
                s_root = sd.resolve()
                fp = (sd / rel).resolve() if rel else s_root
                if fp.is_dir() and (fp == s_root or s_root in fp.parents):
                    return _list_dir_entries(s_root, fp)
                if s_root not in fp.parents or not fp.is_file():
                    return f"存档文件不存在：{rel}"
                return read_text(fp)
            return f"未知读取目标：{target}（请使用 pack:<文件名或目录>、save:<相对路径或目录>、mods、config 或 latest_story）"

        elif name == "update_world_ui":
            pid = tuner_context.get("pack_id") or active_pack_id(sd)
            pdir = pack_dir(pid, sd=sd, required=False)
            if not pdir:
                return "当前尚无已挂载的世界包，无法写入 UI 配置。"

            if _writes_to_mods(sd, pid, tuner_context):
                summary = (args.get("summary") or "已更新世界主题与定制化界面结构").strip()
                try:
                    mods_layer.apply_ui_patch(sd, args.get("theme_patch"), args.get("ui_schema_patch"), label=summary)
                except ValueError as exc:
                    log_event("tuner.ui_rollback", lvl="ERR", pack_id=pid, errors=str(exc), args=args)
                    return f"UI 配置未通过校验（已自动回滚）：\n{exc}"
                tuner_context["ui_updated"] = True
                applied_actions.append({"type": "ui", "summary": f"[UI定制] {summary}"})
                log_event("tuner.ui_updated", pack_id=pid, summary=summary, scope="save")
                return "✓ 已写入本存档的定制并通过校验，世界包没有改动，前端将自动热刷新。"

            theme_path = pdir / "theme.json"
            pack_yaml_path = pdir / "pack.yaml"
            bak_theme = theme_path.read_bytes() if theme_path.exists() else None
            bak_pack = pack_yaml_path.read_bytes() if pack_yaml_path.exists() else None

            theme_patch = args.get("theme_patch")
            ui_patch = args.get("ui_schema_patch")

            if isinstance(theme_patch, dict) and theme_patch:
                cur_theme = {}
                if theme_path.exists():
                    try:
                        cur_theme = json.loads(read_text(theme_path)) or {}
                    except Exception:
                        cur_theme = {}
                for dict_key in ("css_vars", "skyline", "typography", "dialogue_box", "mood_presets"):
                    if isinstance(theme_patch.get(dict_key), dict):
                        merged_sub = dict(cur_theme.get(dict_key) or {})
                        merged_sub.update(theme_patch[dict_key])
                        cur_theme[dict_key] = merged_sub
                if isinstance(theme_patch.get("pixel_frame"), dict):
                    merged_pf = dict(cur_theme.get("pixel_frame") or {})
                    pf_in = theme_patch["pixel_frame"]
                    if isinstance(pf_in.get("palette"), dict):
                        merged_pal = dict(merged_pf.get("palette") or {})
                        merged_pal.update(pf_in["palette"])
                        merged_pf.update(pf_in)
                        merged_pf["palette"] = merged_pal
                    else:
                        merged_pf.update(pf_in)
                    cur_theme["pixel_frame"] = merged_pf
                for k in ("id", "name", "font_body", "font_mono"):
                    if k in theme_patch:
                        cur_theme[k] = theme_patch[k]
                cur_theme.setdefault("id", pid)
                cur_theme = normalize_theme_data(cur_theme)
                theme_path.write_text(json.dumps(cur_theme, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            if isinstance(ui_patch, dict) and ui_patch:
                pmeta, _ = safe_load_yaml(pack_yaml_path)
                pmeta = pmeta or {"id": pid, "name": pid}
                cur_ui = dict(pmeta.get("ui_schema") or {})
                cur_ui.update(ui_patch)
                pmeta["ui_schema"] = cur_ui
                pack_yaml_path.write_text(
                    yaml.safe_dump(pmeta, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )

            rep = doctor_pack(pid)
            if not rep["valid"]:
                if bak_theme is None and theme_path.exists():
                    theme_path.unlink()
                elif bak_theme is not None:
                    theme_path.write_bytes(bak_theme)
                if bak_pack is not None:
                    pack_yaml_path.write_bytes(bak_pack)
                log_event("tuner.ui_rollback", lvl="ERR", pack_id=pid, errors=rep["errors"], args=args)
                return "UI 配置未通过体检（已自动回滚）：\n" + "\n".join(rep["errors"])

            tuner_context["ui_updated"] = True
            summary = (args.get("summary") or "已更新世界主题与定制化界面结构").strip()
            applied_actions.append({"type": "ui", "summary": f"[UI定制] {summary}"})
            log_event("tuner.ui_updated", pack_id=pid, summary=summary)
            return "✓ 已更新世界定制化 UI 并通过 Pack Doctor 体检，前端将自动热刷新。"

        elif name == "update_narrative_style":
            pid = tuner_context.get("pack_id") or active_pack_id(sd)
            pdir = pack_dir(pid, sd=sd, required=False)
            new_tpl = (args.get("new_authors_note_template") or "").strip()
            style_rule = (args.get("style_rule_to_append") or "").strip()
            note = (args.get("note") or "玩家场外文风微调").strip()

            touched = []
            if new_tpl and _writes_to_mods(sd, pid, tuner_context):
                mods_layer.write_files(sd, {"authors_note.md": new_tpl}, label=note)
                touched.append(f"saves/{sd.name}/mods/authors_note.md")
            elif new_tpl:
                cfg = load_config()
                cfg["narrative_prompt"] = {"authors_note_template": new_tpl}
                save_config(cfg)
                touched.append("config.yaml")

            if style_rule and _writes_to_mods(sd, pid, tuner_context):
                mods_layer.append_doc(sd, "style_pack.md", f"- 【场外调教补充】{style_rule}", label=note)
                touched.append(f"saves/{sd.name}/mods/style_pack.md")
            elif style_rule and pdir:
                sp = pdir / "style_pack.md"
                cur = read_text(sp) if sp.exists() else "# 题材文风手册\n"
                sp.write_text(cur.rstrip() + f"\n\n- 【场外调教补充】{style_rule}\n", encoding="utf-8")
                touched.append(f"{pid}/style_pack.md")

            pt_path = ROOT / "engine" / "docs" / "playtest.md"
            if pt_path.exists():
                today = datetime.date.today().isoformat()
                target_str = " & ".join(touched) or "记录备案"
                row = f"| {today} | {pid or '-'} / {sd.name if sd else '-'} | [场外调教] {note} | 已更新 `{target_str}` | 已固化 |\n"
                with open(pt_path, "a", encoding="utf-8") as fh:
                    fh.write(row)

            applied_actions.append({"type": "style", "summary": f"[文风调教] {note}"})
            log_event("tuner.style_updated", pack_id=pid, touched=touched, note=note)
            return "✓ 已更新文风配置，下一回合立即生效。"

        elif name == "update_world_or_save":
            pid = tuner_context.get("pack_id") or active_pack_id(sd)
            pdir = pack_dir(pid, sd=sd, required=False)
            doc_name = (args.get("pack_doc_name") or "").strip()
            doc_append = (args.get("pack_doc_append") or "").strip()
            save_ops = args.get("save_ops") or []
            summary = (args.get("summary") or "已补充世界与存档设定").strip()
            msgs = []

            if doc_name and doc_append:
                if doc_name not in ("setting.md", "rules.md", "protagonist.md", "style_pack.md"):
                    return f"不允许写入非标世界包文件：{doc_name}"
                if not pdir:
                    return "当前未挂载世界包文件夹。"
                if _writes_to_mods(sd, pid, tuner_context):
                    mods_layer.append_doc(sd, doc_name, doc_append, label=summary)
                    msgs.append("已写入本存档的设定补充")
                else:
                    dp = pdir / doc_name
                    cur = read_text(dp) if dp.exists() else ""
                    dp.write_text(cur.rstrip() + "\n\n" + doc_append + "\n", encoding="utf-8")
                    msgs.append("已更新世界包设定文档")

            if save_ops:
                if not sd:
                    return "当前未加载存档，无法执行 save_ops。"
                res = apply_operations(sd, save_ops, rollback_on_error=True, commit_checkpoint=True, checkpoint_prefix="[tuner]")
                if res["rolled_back"]:
                    return "存档修改校验未通过（已原子回滚）：\n" + "\n".join(res["errors"])
                msgs.append("已同步更新存档并通过校验")

            applied_actions.append({"type": "world", "summary": f"[设定调整] {summary}"})
            log_event("tuner.world_updated", pack_id=pid, doc_name=doc_name, ops_count=len(save_ops), summary=summary)
            return "✓ " + ("；".join(msgs) if msgs else "操作完成")

        elif name == "write_mods":
            if not sd:
                return "当前尚未加载存档，无法写入定制。"
            summary = (args.get("summary") or "玩家定制").strip()
            try:
                warnings = mods_layer.write_files(sd, args.get("files"), args.get("panels"), label=summary)
            except ValueError as exc:
                return str(exc)
            tuner_context["ui_updated"] = True
            applied_actions.append({"type": "mod", "summary": f"[定制] {summary}"})
            return "✓ 已写入本存档的定制并通过校验。" + (("\n提醒：\n" + "\n".join(warnings)) if warnings else "")

        elif name == "remove_mod":
            if not sd:
                return "当前尚未加载存档。"
            try:
                mods_layer.remove_mod(sd, args.get("path"))
            except ValueError as exc:
                return str(exc)
            tuner_context["ui_updated"] = True
            applied_actions.append({"type": "mod", "summary": f"[定制] 已移除 {args.get('path')}"})
            return "✓ 已移除。"

        elif name == "request_engine_extension":
            pid = tuner_context.get("pack_id") or active_pack_id(sd)
            mods_layer.record_request(args.get("summary"), args.get("detail"), save=sd.name if sd else "", pack=pid or "")
            applied_actions.append({"type": "request", "summary": f"[扩展需求] {args.get('summary')}"})
            return "已记录。请如实告诉玩家目前做不到，并说明替代做法。"

        elif name == "polish_latest_story":
            polish_cb = tuner_context.get("polish_callback")
            if not polish_cb:
                return "当前无最新故事可润色。"
            old_snip = (args.get("old_snippet") or "").strip()
            new_txt = (args.get("new_text") or "").strip()
            res_msg = polish_cb(old_snip, new_txt)
            applied_actions.append({"type": "polish", "summary": "[正文润色] 已原地更新最新一轮故事措辞"})
            log_event("tuner.story_polished", old_chars=len(old_snip), new_chars=len(new_txt))
            return res_msg

        # ==================== 创世向导 (genesis-onboarding) 工具 ====================
        elif name == "read_pack_raw_files":
            pid = _safe_pack_id(args.get("pack_id"))
            d = PACKS / pid
            if not d.is_dir():
                return f"目录不存在：packs/{pid}"
            chunks = []
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".md", ".txt", ".yaml", ".yml", ".json"):
                    rel = f.relative_to(d).as_posix()
                    chunks.append(f"### `{rel}`\n```\n{read_text(f)[:8000]}\n```")
            log_event("genesis.read_raw", pack_id=pid, files_count=len(chunks))
            return "\n\n".join(chunks) if chunks else "该文件夹下暂无可读文本文档。"

        elif name == "write_pack_bundle":
            pid = _safe_pack_id(args.get("pack_id"))
            files = args.get("files") or {}
            if not isinstance(files, dict) or not files:
                return "files 必须是非空的文件字典。"
            dest = PACKS / pid
            dest.mkdir(parents=True, exist_ok=True)
            written = []
            for fname, content in files.items():
                clean_f = _safe_pack_relpath(fname)
                if not clean_f:
                    continue
                if clean_f == "theme.json":
                    try:
                        parsed_t = json.loads(content) if isinstance(content, str) else content
                        if isinstance(parsed_t, dict):
                            content = json.dumps(normalize_theme_data(parsed_t), ensure_ascii=False, indent=2) + "\n"
                    except Exception:
                        pass
                target = dest / clean_f
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
                written.append(clean_f)

            rep = doctor_pack(pid)
            tuner_context["created_pack_id"] = pid
            log_event("genesis.write_bundle", lvl="INFO" if rep["valid"] else "WARN", pack_id=pid, written=written, valid=rep["valid"], errors=rep["errors"])
            if rep["valid"]:
                applied_actions.append({"type": "genesis_pack", "summary": f"已生成标准世界包 `packs/{pid}/`"})
                return f"✓ 已写入 {written}，世界包 `packs/{pid}/` 通过 Pack Doctor 深度体检！"
            return (
                f"已写入 {written}，但 Pack Doctor 发现以下待修正项（请立即补齐修正后再次调用 write_pack_bundle）：\n"
                + "\n".join(f"✗ {e}" for e in rep["errors"])
            )

        return f"未知工具：{name}"
    except Exception as e:
        log_error("tool.exception", exc=e, role=role, tool=name, args=args)
        if guard is None or type(e).__name__ == "StreamCancelled":
            return f"工具执行异常 ({name})：{e}"
        if name == "apply_changes":
            guard.record_write(False, [f"引擎执行异常：{type(e).__name__}: {e}"])
        if guard.record_fault(name, e) >= guard.REPEAT_LIMIT:
            log_event("tool.circuit_open", lvl="ERR", role=role, tool=name, exc_type=type(e).__name__, exc_msg=str(e)[:500])
            raise EngineToolFault(
                f"工具 {name} 连续 {guard.REPEAT_LIMIT} 次出现同一个引擎内部错误（{type(e).__name__}: {e}），已中止本次调用"
            ) from e
        return (
            f"工具执行异常 ({name})：{type(e).__name__}: {e}\n"
            "参数若没有问题，这就是引擎执行时出的错，换个写法重试没用。"
            "同样的错误再出现一次，本次调用会被引擎中止。"
        )
