"""统一回合调度器（RPGOrchestrator）：解耦前端表现层、热插拔世界包与多后端驱动。

核心职责：
1. 读取无剧透 HUD 状态（委托 `hud_presenter.py` 生成自适应状态组件、角色卡脱敏、手记分栏与面板白名单）
2. 启动自动检测世界包状态（`onboarding_state`），支持四阶段开局引导流水线（驱动自检 → 构想世界 → 孕育种子 → 定制专属 UI）
3. 双通道玩家反馈：
   - 通道 A：主输入框内括号场外指令（由 `context.py` 提取注入当轮 Author's Note）
   - 通道 B：右侧固定独立调教窗口按需唤醒 `tuner-agent`（上下文与主线叙事完全隔离，平时零驻留消耗）
4. 回合调度、原子事务回滚、确定性斜杠命令（`/undo`, `/export`, `/snapshot`, `/retry`）、原地正文润色与划词正/反文风范例入库
"""
import datetime
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from ai_log import (  # noqa: E402
    bind_trace_state,
    current_trace_id,
    log_error,
    log_event,
    new_trace_id,
    reset_trace_state,
    trace_scope,
)
from apply import (  # noqa: E402
    WRITE_LEDGER_ENV,
    apply_operations,
    archive_story_turn,
    git_turn_checkpoint,
    read_write_ledger,
    sync_sliced_indexes,
)
import chapters as chapter_log  # noqa: E402
from clock import ACTIVE, ENGINE_SETTLES_TICK_ENV, load_events  # noqa: E402
from common import (  # noqa: E402
    ROOT,
    SAVES,
    active_pack_id,
    current_save_name,
    day_to_date,
    entity_name,
    load_config,
    load_theme,
    load_ui_schema,
    load_world,
    pack_dir,
    pack_exists,
    read_frontmatter,
    read_text,
    safe_load_yaml,
    save_config,
    save_dir,
    schema_map,
    write_frontmatter,
)
from context import (  # noqa: E402
    assemble_backstage_context,
    assemble_turn_context,
    build_system_prompt,
    format_context_markdown,
    pending_turn_path,
    read_pending_turn,
)
import mods as mods_layer  # noqa: E402
from newsave import build_initial_journal, create_save, overlay_tree, set_current  # noqa: E402
from pack_io import (  # noqa: E402
    doctor_pack,
    export_novel,
    export_pack,
    export_seed,
    flatten_pack,
    import_bundle,
    import_uploaded_payload,
    inspect_pack_detail,
    list_packs,
)
from pack_setup import apply_setup, describe_for_seed, load_setup, resolve_answers, ui_mode  # noqa: E402
import plot  # noqa: E402
from statusline import format_widget_item, get_status_payload  # noqa: E402
import story_log  # noqa: E402
import timeline  # noqa: E402
from turn_protocol import (  # noqa: E402
    INLINE_TAIL,
    MISSING_CHANGES_NOTE,
    RECOVER_CHANGES_ASK,
    ChangesStreamFilter,
    has_changes_block,
    split_changes,
)
from validate import validate  # noqa: E402
import world_candidates as candidates  # noqa: E402

from .api_driver import (  # noqa: E402
    API_PROVIDER_PRESETS,
    FRONT_INLINE_TOOLS_OPENAI,
    ToolRunGuard,
    _is_claude_cli,
    _probe_local_http,
    front_driver_for_turn,
    has_driver_ready,
    httpx,
    is_api_like_driver,
    resolve_back_driver,
    run_openai_compat_loop,
    verify_api_driver,
)
from .cli_driver import discover_installed_clis, run_cli_turn, verify_cli_driver  # noqa: E402
from .driver_config import driver_fingerprint, driver_log_meta, merge_driver_config  # noqa: E402
from .harness_driver import (  # noqa: E402
    REQUIRED_ENTRY_MANIFESTS,
    REQUIRED_SKILLS,
    discover_harnesses,
    run_harness_turn,
    verify_harness_driver,
)
from .hud_presenter import (  # noqa: E402
    ALL_TURNS,
    STORY_PAGE,
    build_onboarding_state,
    build_panel_state,
    get_present_npcs_hud,
    mask_config,
    parse_journal_sections,
    parse_story_History,
    sanitize_player_sheet,
)

PLAN_RETRY_TURNS = 5

COMMAND_ALIASES = {
    "swipe": "retry",
}

OPENING_TURN_LABEL = "【开局序章】"
OPENING_RERUN_INPUT = "请根据当前世界的开局场景、主角身份与在场人物，正式写出序章第一幕。"


def _restore_pending_bytes(sd, snap_bytes):
    p = pending_turn_path(sd)
    if snap_bytes is None:
        if p.exists():
            p.unlink()
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(snap_bytes)


def _snapshot_save_tree(sd: Path) -> dict:
    """记录存档目录（跳过 .git）的文件字节快照，用于拦截并还原只读回合中部分第三方 CLI 的越权直写。"""
    snap = {}
    if not sd or not sd.is_dir():
        return snap
    for p in sd.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        snap[p.relative_to(sd).as_posix()] = p.read_bytes()
    return snap


def _restore_save_tree_if_changed(sd: Path, snap: dict) -> list:
    changed = []
    current_rels = set()
    for p in list(sd.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        rel = p.relative_to(sd).as_posix()
        current_rels.add(rel)
        if rel not in snap:
            changed.append(rel)
            p.unlink(missing_ok=True)
        elif p.read_bytes() != snap[rel]:
            changed.append(rel)
            p.write_bytes(snap[rel])
    for rel, raw in snap.items():
        if rel not in current_rels:
            changed.append(rel)
            target = sd / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    return sorted(set(changed))


class StreamCancelled(Exception):
    """玩家在前端点击「停止生成」或断开 SSE 连接时，在下一次状态/增量回调处抛出，用于触发回合级原子回滚。"""


class WorldSimWriteFailed(RuntimeError):
    """幕后推演想写的改动没有落进存档（最后一次回写失败后再没成功过）。"""


class RPGOrchestrator:
    def __init__(self):
        self.tuner_memory = {}  # 当未加载存档时的内存级独立调教历史
        self._global_lock = threading.RLock()
        self._save_locks = {}

    def _get_save_lock(self, sd) -> threading.RLock:
        key = str(sd.resolve()) if sd else "__nosave__"
        with self._global_lock:
            if key not in self._save_locks:
                self._save_locks[key] = threading.RLock()
            return self._save_locks[key]

    def _tuner_history_path(self, sd):
        return (sd / "log" / "tuner_history.json") if sd else None

    def get_tuner_history(self, save_name=None):
        sd = save_dir(save_name, required=False)
        hp = self._tuner_history_path(sd)
        if hp and hp.exists():
            try:
                data = json.loads(read_text(hp))
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return list(self.tuner_memory.get("__global__", []))

    def _save_tuner_history(self, sd, history):
        trimmed = history[-30:]
        hp = self._tuner_history_path(sd)
        if hp:
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            self.tuner_memory["__global__"] = trimmed

    def clear_tuner_history(self, save_name=None):
        self._refuse_while_job()
        sd = save_dir(save_name, required=False)
        hp = self._tuner_history_path(sd)
        if hp and hp.exists():
            hp.unlink()
        self.tuner_memory.pop("__global__", None)
        return {"ok": True, "tuner_history": []}

    def _build_onboarding_state(self, packs, saves_list, active_pid, cfg=None, theme=None):
        return build_onboarding_state(packs, saves_list, active_pid, cfg=cfg or load_config(), theme=theme)

    @staticmethod
    def _build_panel_state(sd, ui_schema, status_payload, now_meta, journal_text, persona_text, milestones_text):
        return build_panel_state(sd, ui_schema, status_payload, now_meta, journal_text, persona_text, milestones_text)

    @staticmethod
    def _mask_config(cfg):
        return mask_config(cfg)

    def run_system_selfcheck(self):
        """初次启动或按需执行的系统全方位自检：检查运行时、Git、防踩坑技能库、主流 API/CLI/Harness 可用性与世界包状态。"""
        cfg = load_config()
        packs = list_packs()
        sd = save_dir(required=False)
        git_exe = shutil.which("git") or ""

        skills_present = [s for s in REQUIRED_SKILLS if (ROOT / "skills" / s / "SKILL.md").is_file()]
        manifests_present = [m for m in REQUIRED_ENTRY_MANIFESTS if (ROOT / m).is_file()]

        env_keys_detected = [
            env_k
            for env_k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "SILICONFLOW_API_KEY")
            if bool(os.environ.get(env_k, "").strip())
        ]
        ollama_alive = _probe_local_http("http://127.0.0.1:11434/v1/models", timeout=0.35)
        clis = discover_installed_clis()
        harnesses = discover_harnesses()

        checks = [
            {
                "id": "python_runtime",
                "label": "Python 引擎与传输层",
                "ok": sys.version_info >= (3, 9),
                "detail": f"Python {sys.version.split()[0]} · HTTP={'httpx' if httpx else 'urllib(标准库)'}",
            },
            {
                "id": "git_snapshot",
                "label": "Git 回合时光机快照",
                "ok": bool(git_exe),
                "detail": "已安装 Git（支持 Swipe 回合级无损回滚）" if git_exe else "未检测到 Git（将使用纯文件级回退兜底）",
            },
            {
                "id": "skills_manifest",
                "label": "防踩坑技能库与三端入口",
                "ok": len(skills_present) == len(REQUIRED_SKILLS) and len(manifests_present) == len(REQUIRED_ENTRY_MANIFESTS),
                "detail": f"技能 {len(skills_present)}/{len(REQUIRED_SKILLS)} 就绪 · 入口 {', '.join(manifests_present)}",
            },
            {
                "id": "world_packs",
                "label": "独立世界观包健康度",
                "ok": any(p["status"] == "ready" and p["valid"] for p in packs),
                "detail": f"已就绪包 {sum(1 for p in packs if p['status']=='ready')} 个 · 待处理文件夹 {sum(1 for p in packs if p['status']!='ready')} 个",
            },
        ]

        report = {
            "ok": True,
            "checks": checks,
            "driver_ready": has_driver_ready(cfg),
            "driver_verified": has_driver_ready(cfg),
            "driver_verified_at": cfg.get("driver_verified_at", ""),
            "api_presets": API_PROVIDER_PRESETS,
            "env_keys_detected": env_keys_detected,
            "local_ollama_online": ollama_alive,
            "clis": clis,
            "harnesses": harnesses,
            "has_save": bool(sd),
            "active_save": sd.name if sd else "",
        }
        log_event(
            "system.selfcheck",
            driver_ready=report["driver_ready"],
            driver_verified=report["driver_verified"],
            clis_found=[c["id"] for c in clis if c["available"]],
            ollama_online=ollama_alive,
            ready_packs=sum(1 for p in packs if p["status"] == "ready"),
        )
        return report

    def verify_driver_and_save(self, driver_payload: dict, persist: bool = True) -> dict:
        """实测验证玩家配置的 API / CLI / Harness 驱动连通性，成功后自动持久化保存并解锁下一阶段。"""
        self._refuse_while_job()
        with trace_scope("drvchk"):
            cfg = load_config()
            existing_fd = dict(cfg.get("front_driver") or {})
            incoming = dict((driver_payload or {}).get("front_driver") or driver_payload or {})
            incoming.pop("persist", None)
            candidate = merge_driver_config(existing_fd, incoming)

            dtype = (candidate.get("type") or "api").strip().lower()
            candidate["type"] = dtype
            log_event("driver.verify_attempt", role="front", driver=driver_log_meta(candidate), persist=persist)
            if dtype == "cli":
                check_res = verify_cli_driver(candidate)
            elif dtype == "harness":
                candidate["harness_mode"] = (candidate.get("harness_mode") or "external_ide").strip().lower()
                check_res = verify_harness_driver(candidate)
            elif dtype == "api":
                check_res = verify_api_driver(candidate)
            else:
                raise ValueError(f"未知的驱动类型：{dtype}")

            back_check, back_candidate = self._verify_back_driver(cfg, candidate, (driver_payload or {}).get("back_driver"))

            if persist and check_res.get("ok"):
                cfg["front_driver"] = candidate
                if back_candidate is not None and (back_check is None or back_check.get("ok")):
                    cfg["back_driver"] = back_candidate
                cfg["driver_verified"] = check_res.get("web_execution", True) and (back_check is None or bool(back_check.get("ok")))
                cfg["driver_verified_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cfg["driver_verified_for"] = driver_fingerprint(cfg)
                if isinstance((driver_payload or {}).get("narrative_prompt"), dict):
                    cfg["narrative_prompt"] = driver_payload["narrative_prompt"]
                save_config(cfg)

            log_event(
                "driver.verify_outcome", lvl="WARN" if back_check and not back_check.get("ok") else "INFO",
                front_ok=bool(check_res.get("ok")), back_ok=back_check.get("ok") if back_check else None,
                persisted=bool(persist and check_res.get("ok")), web_ready=has_driver_ready(cfg),
                front=driver_log_meta(candidate), back=driver_log_meta(resolve_back_driver(cfg)),
            )
            return {
                "ok": bool(check_res.get("ok")),
                "check": check_res,
                "back_check": back_check,
                "trace_id": current_trace_id(),
                "config": self._mask_config(cfg),
                "selfcheck": self.run_system_selfcheck(),
                "hud": self.get_hud_state(),
            }

    @staticmethod
    def _verify_back_driver(cfg, front_candidate, incoming):
        """实测玩家选的幕后模型。失败不影响台前配置保存，由前端提示玩家改掉或换回同一个模型。"""
        previous = cfg.get("back_driver") or {}
        merged = merge_driver_config(previous, incoming) if isinstance(incoming, dict) else previous
        if merged.get("inherit_front", True):
            return None, merged
        resolved = resolve_back_driver({"front_driver": front_candidate, "back_driver": merged})
        if merged.get("same_endpoint") and front_candidate.get("type") == "harness" and front_candidate.get("harness_mode") == "external_ide":
            return None, merged
        try:
            dtype = resolved.get("type", "api")
            log_event("driver.verify_attempt", role="back", driver=driver_log_meta(resolved))
            verify = {"cli": verify_cli_driver, "harness": verify_harness_driver}.get(dtype, verify_api_driver)
            result = verify(resolved)
            return {"ok": bool(result.get("ok") and result.get("web_execution", True)), "detail": result.get("detail", "")}, merged
        except Exception as exc:
            log_error("driver.back_verify_err", exc=exc)
            return {"ok": False, "detail": str(exc)}, merged

    def import_pack_upload(self, payload: dict) -> dict:
        """接收前端直接拖入的 .rpgpack / .zip / 设定文件夹载荷，执行扁平化与 Pack Doctor 体检并同步状态。"""
        self._refuse_while_job()
        with trace_scope("pkdrop"):
            res = import_uploaded_payload(payload or {})
            cfg = load_config()
            if res.get("import_kind") == "seeded_world":
                sname = res["id"]
                set_current(sname)
                pid = (res.get("pack_id") or "").strip()
                if pid and pack_exists(pid):
                    cfg["active_pack"] = pid
                    save_config(cfg)
                res["hud"] = self.get_hud_state(sname)
                return res

            pid = res["id"]
            doc = res.get("doctor") or {}
            if doc.get("status") == "ready":
                cfg["active_pack"] = pid
                save_config(cfg)
            res["hud"] = self.get_hud_state()
            return res

    def get_hud_state(self, save_name=None):
        cfg = load_config()
        sd = save_dir(save_name, required=False)
        packs = list_packs()
        saves_list, pending_worlds = self._list_saves()

        if not sd:
            pid = active_pack_id(None)
            active_pack_meta = next((p for p in packs if p["id"] == pid), None) or {}
            theme_no_save = active_pack_meta.get("theme") or {}
            onboarding = self._build_onboarding_state(packs, saves_list, pid, cfg=cfg, theme=theme_no_save)
            return {
                "has_save": False,
                "pack_id": pid,
                "pack_missing": False,
                "missing_pack_id": "",
                "active_pack_meta": active_pack_meta,
                "world_title": active_pack_meta.get("name") or "AI-RPG-PLAYER · 待开启世界",
                "theme": theme_no_save,
                "ui_schema": load_ui_schema(None, pid),
                "onboarding": onboarding,
                "config": self._mask_config(cfg),
                "api_presets": API_PROVIDER_PRESETS,
                "packs": packs,
                "saves": saves_list,
                "pending_worlds": pending_worlds,
                "statusline": get_status_payload(None),
                "tuner_history": self.get_tuner_history(None),
                "job": self.get_job_state()["job"],
            }

        status_payload = get_status_payload(sd.name)
        try:
            now_meta, now_body = read_frontmatter(sd / "world" / "now.md")
        except Exception:
            now_meta, now_body = {}, ""
        now_meta = now_meta or {}
        world_meta = load_world(sd)
        declared_pack = str(world_meta.get("pack") or "").strip()
        is_pack_missing = bool(declared_pack and not pack_exists(declared_pack))
        pid = declared_pack if (declared_pack and not is_pack_missing) else active_pack_id(sd)
        ui_schema = load_ui_schema(sd, pid)
        active_pack_meta = next((p for p in packs if p["id"] == pid), None) or {}
        theme = load_theme(sd, pid)

        journal_text = read_text(sd / "player" / "journal.md") if (sd / "player" / "journal.md").exists() else ""
        persona_text = read_text(sd / "player" / "persona.md") if (sd / "player" / "persona.md").exists() else ""
        milestones_text = read_text(sd / "log" / "milestones.md") if (sd / "log" / "milestones.md").exists() else ""
        pack_title = active_pack_meta.get("name") or declared_pack or "独立世界"
        save_world_name = world_meta.get("name") or sd.name

        entity_counts = {
            k: len(list((sd / "world" / k).glob("*.md"))) if (sd / "world" / k).is_dir() else 0
            for k in ("npcs", "locations", "factions")
        }

        return {
            "has_save": True,
            "save_name": sd.name,
            "world_name": save_world_name,
            "world_title": pack_title if pack_title == save_world_name else f"{pack_title} · {save_world_name}",
            "pack_id": pid,
            "pack_missing": is_pack_missing,
            "missing_pack_id": declared_pack if is_pack_missing else "",
            "active_pack_meta": active_pack_meta,
            "entity_counts": entity_counts,
            "theme": theme,
            "ui_schema": ui_schema,
            "onboarding": self._build_onboarding_state(packs, saves_list, pid, cfg=cfg, theme=theme),
            "config": self._mask_config(cfg),
            "api_presets": API_PROVIDER_PRESETS,
            "packs": packs,
            "saves": saves_list,
            "pending_worlds": pending_worlds,
            "statusline": status_payload,
            "now": {
                "day": int(now_meta.get("day", 0)),
                "volume": int(now_meta.get("volume", 1)),
                "location": now_meta.get("location"),
                "present_npcs": get_present_npcs_hud(sd, now_meta.get("present")),
                "frozen": bool(now_meta.get("frozen")),
                "ui_mood": str(now_meta.get("ui_mood") or "default"),
                "recent_challenges": now_meta.get("recent_challenges") or [],
                "body": (now_body or "").strip(),
            },
            "player": sanitize_player_sheet(sd, ui_schema),
            "journal": journal_text.strip(),
            "journal_cards": parse_journal_sections(journal_text, ui_schema),
            "persona": persona_text.strip(),
            "milestones": milestones_text.strip(),
            "story_turns": parse_story_History(sd, limit=STORY_PAGE),
            "story_total": story_log.count_turns(sd),
            "tuner_history": self.get_tuner_history(sd.name),
            "mods": mods_layer.list_mods(sd),
            **self._seed_state(sd),
            "job": self.get_job_state()["job"],
            "panel_state": self._build_panel_state(sd, ui_schema, status_payload, now_meta, journal_text, persona_text, milestones_text),
        }

    def _list_saves(self):
        """玩家能看到的存档，以及重新生成后等玩家决定的世界。候选存档不进存档列表。"""
        saves, pending = [], []
        if not SAVES.is_dir():
            return saves, pending
        cur_name = current_save_name()
        job = self.__dict__.get("_job") or {}
        for d in sorted(SAVES.iterdir()):
            if not save_dir(d.name, required=False):
                continue
            world = load_world(d)
            unfinished = self._seed_marker(d).exists()
            replaces = candidates.replaces_of(d)
            if not replaces:
                saves.append({
                    "name": d.name,
                    "pack": world.get("pack", ""),
                    "genre": world.get("genre", ""),
                    "is_current": d.name == cur_name,
                    "state": "interrupted" if unfinished else ("world" if candidates.has_world(d) else "empty"),
                })
            elif job.get("save") != d.name:
                old = SAVES / replaces
                pending.append({
                    "name": d.name,
                    "replaces": replaces,
                    "pack": world.get("pack", ""),
                    "state": "interrupted" if unfinished else "ready",
                    "error": self._seed_state(d)["seed_error"],
                    "counts": {k: len(list((d / "world" / k).glob("*.md"))) for k in ("factions", "locations", "npcs")},
                    "old_turns": len(parse_story_History(old, limit=ALL_TURNS)) if old.is_dir() else 0,
                })
        return saves, pending

    def resolve_pending_world(self, name, action):
        """玩家决定怎么处理重新生成的世界：use_new 用新的 / keep_old 保留旧的 / keep_both 都留下。"""
        self._refuse_while_job()
        sd = SAVES / candidates.clean_save_name(name)
        if not candidates.replaces_of(sd):
            raise ValueError(f"没有等待决定的世界：{name}")
        if action == "keep_old":
            replaces = candidates.replaces_of(sd)
            candidates.discard(sd)
            log_event("world_candidate.discarded", replaces=replaces)
            return {"ok": True, "save_name": replaces, "backup": "", "hud": self.get_hud_state()}
        if action not in ("use_new", "keep_both"):
            raise ValueError(f"未知操作：{action}")
        if self._seed_marker(sd).exists():
            raise ValueError("这个世界还没有生成完，不能使用。可以继续生成，或者放弃这次。")

        backup = ""
        if action == "use_new":
            final_name, backup = candidates.adopt(sd)
        else:
            final_name = candidates.keep_both(sd)
        set_current(final_name)
        final_sd = SAVES / final_name
        try:
            export_seed(final_sd)
        except Exception as exc:
            log_error("world_candidate.export_err", exc=exc)
        log_event("world_candidate.resolved", action=action, save=final_name, backup=backup)
        return {"ok": True, "save_name": final_name, "backup": backup, "hud": self.get_hud_state(final_name)}

    def get_panel_source(self, panel_id, save_name=None):
        sd = save_dir(save_name, required=False)
        if not sd:
            raise ValueError("当前尚未加载存档")
        return {"id": panel_id, "html": mods_layer.panel_source(sd, panel_id)}

    def remove_mod(self, path, save_name=None):
        self._refuse_while_job()
        sd = save_dir(save_name)
        with self._get_save_lock(sd):
            mods_layer.remove_mod(sd, path)
        return {"ok": True, "hud": self.get_hud_state(sd.name)}

    def select_pack(self, pack_id, auto_switch_save=True):
        """在 Web 端切换当前激活的世界观包（同步更新 config.yaml，若存在该包的存档则自动切换过去）。"""
        d = pack_dir(pack_id, required=False)
        if not d or not d.is_dir():
            raise ValueError(f"世界包不存在：packs/{pack_id}")
        cfg = load_config()
        cfg["active_pack"] = pack_id
        save_config(cfg)

        switched_save = None
        if auto_switch_save and SAVES.is_dir():
            for sdir in sorted(SAVES.iterdir()):
                if save_dir(sdir.name, required=False) and not candidates.is_candidate(sdir):
                    w = load_world(sdir)
                    if w.get("pack") == pack_id:
                        set_current(sdir.name)
                        switched_save = sdir.name
                        break
        return {
            "ok": True,
            "pack_id": pack_id,
            "switched_save": switched_save,
            "hud": self.get_hud_state(switched_save),
        }

    def get_pack_detail(self, pack_id):
        return inspect_pack_detail(pack_id)

    def run_pack_doctor(self, pack_id):
        return doctor_pack(pack_id)

    def run_pack_flatten(self, pack_id):
        self._refuse_while_job()
        res = flatten_pack(pack_id)
        res["hud"] = self.get_hud_state()
        return res

    @staticmethod
    def _pack_command_file(sd, cmd_name):
        if not sd or not cmd_name or not re.fullmatch(r"[A-Za-z0-9_-]+", str(cmd_name)):
            return None
        pdir = pack_dir(sd=sd, required=False)
        for folder in (sd / "mods", pdir):
            path = folder / "commands" / f"{cmd_name}.md" if folder else None
            if path and path.is_file():
                return path
        return None

    def _command_counts_as_turn(self, sd, cmd_name):
        """普通行动、/tick、/newworld 算回合；世界包命令可用 archive: true 声明自己也算回合。"""
        if cmd_name in (None, "tick", "newworld"):
            return True
        path = self._pack_command_file(sd, cmd_name)
        if not path:
            return False
        meta, _ = read_frontmatter(path)
        return bool((meta or {}).get("archive"))

    def expand_slash_command(self, user_input, sd=None):
        """如果是 /斜杠指令，展开为结构化指令 Prompt。世界包自带的命令优先于引擎中立命令目录。"""
        text = (user_input or "").strip()
        if not text.startswith("/"):
            return None, text
        parts = text[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else ""
        cmd_name = COMMAND_ALIASES.get(cmd_name, cmd_name)

        cmd_file = self._pack_command_file(sd, cmd_name)
        if not cmd_file and re.fullmatch(r"[A-Za-z0-9_-]+", cmd_name):
            neutral = ROOT / "engine" / "commands" / f"{cmd_name}.md"
            cmd_file = neutral if neutral.exists() else (ROOT / ".claude" / "commands" / f"{cmd_name}.md")
        if not cmd_file or not cmd_file.exists():
            return cmd_name, text
        _, body = read_frontmatter(cmd_file)
        expanded = (body or read_text(cmd_file)).replace("$ARGUMENTS", cmd_args).strip()
        return cmd_name, expanded

    def cancel_active_streams(self):
        """玩家点击「停止生成」：通知所有进行中的流式任务在下一个状态/增量回调处中止（回合内会触发原子回滚）。"""
        evs = list(getattr(self, "_active_cancel_events", set()) or [])
        for ev in evs:
            ev.set()
        log_event("stream.cancel_requested", active=len(evs))
        return {"ok": True, "cancelled": len(evs)}

    # ---------- 长任务：同一时间只跑一个，进度和结果可以随时查询 ----------
    JOB_LABELS = {"conceive": "设计世界", "seed": "生成世界", "customize_ui": "定制界面"}

    def _begin_job(self, op, save="", world="", replaces="", pack=""):
        with self.__dict__.setdefault("_job_lock", threading.Lock()):
            job = self.__dict__.get("_job")
            if job:
                raise ValueError(f"「{job['label']}」还没做完，等它完成或取消后再试。")
            self._job = {
                "op": op, "label": self.JOB_LABELS.get(op, op), "save": save, "world": world,
                "replaces": replaces, "pack": pack, "phase": "", "started_at": time.time(),
            }

    def _end_job(self, ok=True, error="", cancelled=False, result=None):
        job = self.__dict__.get("_job") or {}
        self._last_job = {**job, "ok": ok, "error": error, "cancelled": cancelled, "result": result, "finished_at": time.time()}
        self._job = None

    def refuse_while_job(self):
        self._refuse_while_job()

    def _refuse_while_job(self):
        job = self.__dict__.get("_job")
        if job:
            raise ValueError(f"「{job['label']}」还没做完，完成后才能继续。")

    def get_job_state(self):
        """进度只报数量，不报内容，不会剧透。"""
        job = self.__dict__.get("_job")
        out = {"job": None, "last": self.__dict__.get("_last_job")}
        if job:
            counts = {}
            if job.get("save"):
                world = SAVES / job["save"] / "world"
                counts = {k: len(list((world / k).glob("*.md"))) for k in ("factions", "locations", "npcs")}
            out["job"] = {**job, "elapsed": int(time.time() - job["started_at"]), "counts": counts}
        return out

    @staticmethod
    def _seed_marker(sd):
        return Path(sd) / "world" / "seed_status.yaml"

    def _mark_seed_failed(self, sd, error):
        marker = self._seed_marker(sd)
        meta, _ = safe_load_yaml(marker)
        marker.write_text(
            yaml.safe_dump({**(meta if isinstance(meta, dict) else {}), "state": "failed", "error": str(error)[:500]}, allow_unicode=True),
            encoding="utf-8",
        )

    def _plan_seed_target(self, payload):
        pid = (payload.get("pack_id") or active_pack_id() or "").strip()
        if not pack_dir(pid, required=False):
            raise ValueError("请先在阶段一生成或选择一个标准世界包")
        if not (payload.get("world_name") or "").strip():
            raise ValueError("请输入新世界存档的名称")
        name = candidates.clean_save_name(payload.get("world_name"))
        existing = SAVES / name
        plan = {"pack": pid, "world": name, "save": name, "replaces": "", "mode": "new"}
        if not existing.is_dir():
            return plan
        if candidates.is_candidate(existing):
            raise ValueError(f"「{name}」这个名字正被一次重新生成占用，请换一个名字。")
        declared = str(load_world(existing).get("pack") or "").strip()
        if declared and declared != pid:
            raise ValueError(f"「{name}」属于另一个世界（{declared}），不能混在一起。要玩这个世界，请换一个名字。")
        if self._seed_marker(existing).exists() or not candidates.has_world(existing):
            return {**plan, "mode": "resume"}

        pending = candidates.find_for(name)
        if pending is None:
            return {**plan, "mode": "candidate_new", "replaces": name, "save": candidates.free_name(f"{name}（重新生成）")}
        if not self._seed_marker(pending).exists():
            raise ValueError(f"「{name}」上次重新生成的结果还没决定，请先选择用哪个。")
        return {**plan, "mode": "candidate_resume", "replaces": name, "save": pending.name}

    def _seed_state(self, sd):
        marker = self._seed_marker(sd)
        if not marker.exists():
            return {"seed_state": "", "seed_error": ""}
        job = self.__dict__.get("_job") or {}
        if job.get("op") == "seed" and job.get("save") == sd.name:
            return {"seed_state": "running", "seed_error": ""}
        meta, _ = safe_load_yaml(marker)
        return {"seed_state": "interrupted", "seed_error": str((meta or {}).get("error") or "")}

    def _stream_via_worker(self, op_name, ctx_meta, worker_fn, detach=False):
        """在独立工作线程中绑定追踪号 `tid` 运行任务，通过无锁队列实时向外层 SSE 生成器产出无剧透状态与正文增量。"""
        tid = new_trace_id(op_name[:4] or "op")
        q = queue.Queue()
        sentinel = object()
        cancel_event = threading.Event()
        if not hasattr(self, "_active_cancel_events"):
            self._active_cancel_events = set()
        self._active_cancel_events.add(cancel_event)

        def emit(event_dict):
            if cancel_event.is_set():
                raise StreamCancelled("玩家已手动停止本轮生成")
            if isinstance(event_dict, dict):
                q.put(event_dict)

        def _target():
            ctx = {"op": op_name}
            for k, v in (ctx_meta or {}).items():
                if v is not None and v != "":
                    ctx[k] = v
            tok = bind_trace_state(tid, ctx)
            t0 = time.perf_counter()
            log_event(f"{op_name}.begin", **(ctx_meta or {}))
            try:
                res = worker_fn(emit)
                ms = round((time.perf_counter() - t0) * 1000, 1)
                log_event(f"{op_name}.end", ms=ms)
                q.put({"type": "done", "result": res, "trace_id": tid})
            except StreamCancelled as exc:
                ms = round((time.perf_counter() - t0) * 1000, 1)
                log_event(f"{op_name}.cancelled", lvl="WARN", ms=ms, reason=str(exc))
                q.put({"type": "error", "detail": str(exc), "trace_id": tid, "cancelled": True})
            except Exception as exc:
                ms = round((time.perf_counter() - t0) * 1000, 1)
                log_error(f"{op_name}.err", exc=exc, ms=ms)
                q.put({"type": "error", "detail": str(exc), "trace_id": tid})
            finally:
                reset_trace_state(tok)
                self._active_cancel_events.discard(cancel_event)
                q.put(sentinel)

        th = threading.Thread(target=_target, daemon=True)
        th.start()
        finished = False
        try:
            while True:
                item = q.get()
                if item is sentinel:
                    finished = True
                    break
                yield item
        finally:
            if not finished and not detach:
                cancel_event.set()

    def run_world_sim(self, sd, args, cfg, on_status=None, settle_by_engine=False):
        """在独立上下文中执行幕后主持人 world-sim（仅推送无剧透状态码，绝不向前端暴露推演内容或变动文件）。

        settle_by_engine=True：引擎安排的推演，进度（last_tick_day）由引擎在验收后记录，模型的 mark_ticked 不生效。
        有改动没写进存档（最后一次回写失败之后再没成功过）时抛 WorldSimWriteFailed，不把模型的简报当成完成。
        """
        driver_cfg = {**resolve_back_driver(cfg), "save_name": sd.name}
        guard = ToolRunGuard(engine_settles_tick=settle_by_engine)
        ledger_path = None
        if driver_cfg.get("type") == "cli":
            # 命令行驱动看不到工具调用：让 apply.py 把每次回写的成败记进临时文件
            ledger_fd, ledger_path = tempfile.mkstemp(prefix="rpg_write_ledger_", suffix=".jsonl")
            os.close(ledger_fd)
            driver_cfg["engine_env"] = {
                **(driver_cfg.get("engine_env") or {}),
                WRITE_LEDGER_ENV: ledger_path,
                **({ENGINE_SETTLES_TICK_ENV: "1"} if settle_by_engine else {}),
            }

        if on_status:
            on_status({"type": "status", "phase": "world_sim", "label": "🌐 幕后世界正在悄然演化…", "world_sim": True})

        is_api = is_api_like_driver(driver_cfg)
        sys_prompt = build_system_prompt(sd, role="back", protocol="api" if is_api else "tools")
        task_type = str(args.get("task_type") or "推演")
        sim_t0 = time.perf_counter()
        is_seed = ("种子" in task_type) or ("开局" in task_type)
        pre_ctx = "" if is_seed else assemble_backstage_context(sd)
        task_prompt = (
            f"请执行幕后主持人（world-sim）任务：\n"
            f"- 当前存档：saves/{sd.name}/（只读写这个存档和它的世界包，不要打开其他存档或世界包）\n"
            f"- 任务类型：{task_type}\n"
            f"- 时间范围：{args.get('day_range', '当前')}\n"
            f"- 具体要求：{args.get('request', '')}\n"
            f"请按规范修改存档文件、运行校验并返回无剧透简报。"
            + (f"\n\n---\n\n{pre_ctx}" if pre_ctx else "")
        )
        log_event(
            "world_sim.invoke", save=sd.name, task_type=task_type, day_range=args.get("day_range", "当前"),
            driver=driver_log_meta(driver_cfg), system_chars=len(sys_prompt), context_chars=len(pre_ctx),
            prompt_chars=len(task_prompt),
        )
        try:
            if driver_cfg.get("type") == "cli":
                res = run_cli_turn(
                    driver_cfg,
                    task_prompt,
                    role="back",
                    stream=False,
                    on_status=on_status,
                    system_prompt=sys_prompt,
                )
                for ok, errors in read_write_ledger(ledger_path):
                    guard.record_write(ok, errors)
            elif driver_cfg.get("type") == "harness":
                res = run_harness_turn(
                    driver_cfg,
                    sys_prompt,
                    task_prompt,
                    role="back",
                    stream=False,
                    on_status=on_status,
                    sd=sd,
                    max_steps=50 if is_seed else 30,
                    tool_guard=guard,
                )
            else:
                res = run_openai_compat_loop(
                    driver_cfg,
                    sys_prompt,
                    [{"role": "user", "content": task_prompt}],
                    sd,
                    role="back",
                    max_steps=50 if is_seed else 30,
                    stream=bool(on_status),
                    on_status=on_status,
                    on_delta=None,
                    tool_guard=guard,
                )
        finally:
            if ledger_path:
                Path(ledger_path).unlink(missing_ok=True)
        brief_out = res["text"]
        ms = round((time.perf_counter() - sim_t0) * 1000, 1)
        if guard.unresolved_write_errors:
            log_event(
                "world_sim.write_failed", lvl="ERR", save=sd.name, task_type=task_type, ms=ms,
                writes_ok=guard.writes_ok, writes_failed=guard.writes_failed, errors=guard.unresolved_write_errors,
            )
            raise WorldSimWriteFailed(f"幕后的改动没有写进存档：{guard.unresolved_write_errors[0]}")
        log_event(
            "world_sim.done", save=sd.name, task_type=task_type, brief_chars=len(brief_out or ""), ms=ms,
            writes_ok=guard.writes_ok, writes_failed=guard.writes_failed,
        )
        return brief_out

    # ==================== 右侧独立场外调教助手 (On-Demand tuner-agent) ====================

    def _polish_story_helper(self, sd, old_snippet, new_text):
        turns = parse_story_History(sd, limit=2)
        if not turns:
            raise ValueError("当前尚无已归档的故事正文可供润色")
        last = turns[-1]
        p = sd / last["file"]
        raw = read_text(p)
        old_story = last["story"]
        if old_story not in raw:
            raise ValueError("未能在故事档案中定位最新段落")
        if old_snippet and old_snippet in old_story:
            updated_story = old_story.replace(old_snippet, new_text, 1)
        else:
            updated_story = new_text
        p.write_text(raw.replace(old_story, updated_story, 1), encoding="utf-8")
        git_turn_checkpoint(sd, "tuner-agent 局部润色正文", prefix="[polish]")
        return "✓ 已原地替换更新最近一轮故事正文（未回滚游戏状态）。"

    def _run_tuner_core(self, feedback_text, save_name=None, stream=False, on_status=None, on_delta=None):
        msg = (feedback_text or "").strip()
        if not msg:
            raise ValueError("请输入需要调教或定制的内容")
        self._refuse_while_job()
        sd = save_dir(save_name, required=False)
        with (self._get_save_lock(sd) if sd else threading.RLock()):
            return self._run_tuner_locked(msg, sd, stream, on_status, on_delta)

    def _run_tuner_locked(self, msg, sd, stream, on_status, on_delta):
        cfg = load_config()
        mods_before = None
        pid = active_pack_id(sd)
        driver_cfg = {**front_driver_for_turn(cfg), "save_name": sd.name if sd else ""}
        is_api = is_api_like_driver(driver_cfg)
        ui_before = (load_theme(sd, pid), load_ui_schema(sd, pid))

        turns = parse_story_History(sd, limit=1) if sd else []
        latest_story = turns[-1]["story"] if turns else ""

        tuner_ctx = {
            "pack_id": pid,
            "latest_story": latest_story,
            "applied_actions": [],
            "ui_updated": False,
            "polish_callback": (lambda o, n: self._polish_story_helper(sd, o, n)) if sd else None,
        }

        sys_prompt = build_system_prompt(sd, role="tuner", pack_id=pid, protocol="api" if is_api else "tools")
        history = self.get_tuner_history(sd.name if sd else None)
        chat_msgs = []
        for h in history[-8:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                chat_msgs.append({"role": h["role"], "content": h["content"]})

        state_routing = (
            f"界面、文风、规则、命令、面板这类对世界包的覆盖写入 `saves/{sd.name}/mods/`（不改世界包）。"
            f"调教当前世界本身的状态不放 mods，直接改存档文件：主角性格、喜好、感情取向（心性）改 `player/persona.md`，主角数值改 `player/sheet.md`，"
            f"主角知道的事改 `player/journal.md`，某个人物改 `world/npcs/<id>.md`，剧情走向改 `world/director.md`，世界事实改 `world/canon.md`。"
            if sd else f"当前未加载存档，所有 UI 与文风定制直接写入世界包 `packs/{pid}/`。"
        )
        external_target = (
            state_routing
            + (
                f" 改 `mods/` 后运行 `python engine/tools/mods.py check --save {sd.name}`；"
                f"改存档状态用 `python engine/tools/apply.py --save {sd.name} --json '[…]'` 提交（不放 mods），界面上才看得到。"
                if sd else ""
            )
        )
        api_target = (
            state_routing
            + (
                " 改存档状态请调用 `update_world_or_save` 的 `save_ops`（不放 mods），界面上才看得到；改定制层调用 `write_mods` 或 `update_world_ui`。"
                if sd else ""
            )
        )

        anti_spoiler_tail = (
            "\n\n---\n回复给玩家看，不要剧透：只说玩家已经知道的（他自己定的设定、正文里发生过的事、界面上看得到的）。"
            "幕后文件、人物卡里的真名、真实修为、来历、秘密，以及还没发生的事，读到了也不要写进回复。"
        )
        base_brief = (
            f"【当前场外环境快照】\n"
            f"- 当前世界包：`{pid or '未挂载'}`\n"
            f"- 当前存档：`{sd.name if sd else '未加载'}`\n"
            + (f"- 本存档已有的定制：{json.dumps(mods_layer.list_mods(sd), ensure_ascii=False)}\n" if sd else "")
            + f"- 当前通用叙事提示词：`{((cfg.get('narrative_prompt') or {}).get('authors_note_template') or '')[:240]}...`\n"
            + (f"- 最近一轮正文摘录：\n```\n{latest_story[:1200]}\n```\n" if latest_story else "")
        )
        state_brief = f"{base_brief}\n---\n【玩家场外反馈 / 定制诉求】\n{msg}{anti_spoiler_tail}"
        api_user_brief = f"{api_target}\n\n{base_brief}\n---\n【玩家场外反馈 / 定制诉求】\n{msg}{anti_spoiler_tail}"
        chat_msgs.append({"role": "user", "content": api_user_brief})

        if sd:
            timeline.commit_pending(sd)
            mods_before = mods_layer.snapshot(sd)

        if driver_cfg.get("type") == "cli":
            cli_prompt = (
                f"你现在作为独立场外调教助手 tuner-agent 按需唤醒（遵循 skills/live-tuner/SKILL.md 与 skills/ui-designer/SKILL.md）。\n"
                f"请勿修改 web/static/ 固定系统外壳代码。{external_target}\n\n"
                f"{state_brief}"
            )
            res = run_cli_turn(driver_cfg, cli_prompt, role="tuner", stream=stream, on_status=on_status, on_delta=on_delta, system_prompt=sys_prompt)
        elif driver_cfg.get("type") == "harness":
            harness_prompt = (
                api_user_brief
                if is_api
                else (
                    f"你现在作为独立场外调教助手 tuner-agent 按需唤醒（遵循 skills/live-tuner/SKILL.md 与 skills/ui-designer/SKILL.md）。\n"
                    f"请勿修改 web/static/ 固定宏观布局代码。{external_target}\n\n"
                    f"{state_brief}"
                )
            )
            res = run_harness_turn(
                driver_cfg,
                sys_prompt,
                harness_prompt,
                role="tuner",
                stream=stream,
                on_status=on_status,
                on_delta=on_delta,
                sd=sd,
                tuner_context=tuner_ctx,
                max_steps=10,
            )
        else:
            res = run_openai_compat_loop(
                driver_cfg,
                sys_prompt,
                chat_msgs,
                sd,
                role="tuner",
                tuner_context=tuner_ctx,
                max_steps=10,
                stream=stream,
                on_status=on_status,
                on_delta=on_delta,
            )

        reply = (res.get("text") or "").strip()
        mod_errors = []
        if mods_before is not None:
            mod_errors, _ = mods_layer.check_mods(sd)
            if mod_errors:
                mods_layer.restore_snapshot(sd, mods_before)
                tuner_ctx["applied_actions"] = []
                reply += "\n\n这次的定制没有通过校验，已恢复原样：\n" + "\n".join(mod_errors)
            else:
                git_turn_checkpoint(sd, "调教助手定制", prefix="[tuner]")
        if not reply and not tuner_ctx["applied_actions"]:
            raise ValueError("调教没有返回回复，也没有已应用的操作")
        now_ts = datetime.datetime.now().strftime("%H:%M")
        history.append({"role": "user", "content": msg, "time": now_ts})
        history.append({
            "role": "assistant",
            "content": reply,
            "time": now_ts,
            "actions": tuner_ctx["applied_actions"],
        })
        self._save_tuner_history(sd, history)

        return {
            "ok": not mod_errors,
            "reply": reply,
            "applied_actions": tuner_ctx["applied_actions"],
            "ui_updated": ui_before != (load_theme(sd, pid), load_ui_schema(sd, pid)),
            "trace_id": current_trace_id(),
            "hud": self.get_hud_state(sd.name if sd else None),
        }

    def run_tuner(self, feedback_text, save_name=None):
        """按需单次唤醒右侧独立调教 Sub-Agent（平时零驻留，与左侧台前故事流完全隔离）。"""
        sd = save_dir(save_name, required=False)
        with trace_scope("tuner", save=sd.name if sd else "", pack=active_pack_id(sd)):
            return self._run_tuner_core(feedback_text, save_name=save_name, stream=False)

    def stream_tuner(self, feedback_text, save_name=None):
        """流式版场外调教助手：实时推送无剧透处理状态与打字机回复。"""
        sd = save_dir(save_name, required=False)
        return self._stream_via_worker(
            "tuner",
            {"save": sd.name if sd else "", "pack": active_pack_id(sd)},
            lambda emit: self._run_tuner_core(
                feedback_text,
                save_name=save_name,
                stream=True,
                on_status=emit,
                on_delta=lambda txt: emit({"type": "delta", "text": txt}),
            ),
        )

    # ==================== 四阶段创世引导工作流 (genesis-onboarding) ====================

    def _run_genesis_core(self, stage, payload, on_status=None, plan=None):
        cfg = load_config()
        driver_cfg = cfg.get("front_driver") or {}

        if stage == "conceive":
            raw_pid = (payload.get("pack_id") or payload.get("source_pack_id") or "new_world").strip()
            pid = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", raw_pid).strip("_") or "new_world"
            concept = (payload.get("concept") or "").strip()
            source_pack = (payload.get("source_pack_id") or "").strip()
            if not concept and not source_pack:
                raise ValueError("请描述你的世界构想，或选择一个待解析的设定文件夹")

            if on_status:
                on_status({"type": "status", "phase": "thinking", "label": "正在构思世界架构与规则体系…", "world_sim": False})

            driver_cfg = resolve_back_driver(cfg)
            is_api = is_api_like_driver(driver_cfg)
            sys_prompt = build_system_prompt(None, role="genesis", pack_id=source_pack or None, protocol="api" if is_api else "tools")
            user_prompt = (
                f"请执行【创世向导 · 阶段一：构想世界并生成标准规则包】。\n"
                f"- 目标世界包目录 ID：`{pid}`\n"
                + (f"- 待解析的散装设定源文件夹：`packs/{source_pack}/`（请先读取其原始文档内容）\n" if source_pack else "")
                + f"- 玩家的世界构想与偏好：\n{concept or '请根据源文件夹内容提炼构建完整自洽的世界规则包'}\n\n"
                f"请立即设计并在 `packs/{pid}/` 下一次性生成以下标准文件（注意：当前为单次构建模式，对玩家未指明的细节请直接发挥主编创意自主补全，切勿停下反问！）：\n"
                f"1. `pack.yaml`（含 id, name, genre, description, calendar 及贴合题材的基础 ui_schema；`calendar.start_year` 写世界已经延续到的年份，不要写 1）\n"
                f"2. `setting.md`（世界基调、三大核心冲突、势力格局与地理风貌，精炼总纲 2KB~8KB）\n"
                f"3. `rules.md`（难关裁决原则、成长阶梯与资源体系说明）\n"
                f"4. `rules.yaml`（含 schema_mapping, levels, ranks[带attr_range], attributes, attitude_axes, gear_tiers, difficulty）\n"
                f"5. `protagonist.md`（主角起手身份、初始物品、人际钩子与身世伏笔）\n"
                f"6. `style_pack.md`（环境感官词库与叙事腔调要求）\n"
                f"确保通过 Pack Doctor 体检后，用精炼中文向玩家汇报已生成的世界设定亮点（严禁剧透深层黑幕）。"
            )
            existing = pack_dir(pid, required=False)
            before = _snapshot_save_tree(existing) if existing else {}
            tctx = {"applied_actions": []}
            if driver_cfg.get("type") == "cli":
                res = run_cli_turn(driver_cfg, user_prompt, role="genesis", on_status=on_status, system_prompt=sys_prompt)
            elif driver_cfg.get("type") == "harness":
                res = run_harness_turn(
                    driver_cfg, sys_prompt, user_prompt, role="genesis", on_status=on_status, tuner_context=tctx, max_steps=12
                )
            else:
                res = run_openai_compat_loop(
                    driver_cfg, sys_prompt, [{"role": "user", "content": user_prompt}], None,
                    role="genesis", tuner_context=tctx, max_steps=12, stream=bool(on_status), on_status=on_status
                )

            final_pid = tctx.get("created_pack_id") or pid
            rep = doctor_pack(final_pid)
            if final_pid != pid or before == _snapshot_save_tree(pack_dir(final_pid, required=False)):
                rep["errors"].append("没有为指定目标生成新的世界包内容")
                rep["valid"] = False
            if rep["valid"]:
                cfg["active_pack"] = final_pid
                save_config(cfg)

            return {
                "ok": rep["valid"],
                "stage": "conceive",
                "pack_id": final_pid,
                "doctor": rep,
                "summary": res.get("text", ""),
                "trace_id": current_trace_id(),
                "hud": self.get_hud_state(None),
            }

        elif stage == "seed":
            plan = plan or self._plan_seed_target(payload)
            pid, world_name, mode = plan["pack"], plan["world"], plan["mode"]
            protagonist_pref = (payload.get("protagonist_pref") or "").strip()

            if on_status:
                on_status({"type": "status", "phase": "seed_init", "label": "正在初始化新世界骨架…", "world_sim": False})

            setup_answers = payload.get("setup_answers")
            setup_items = load_setup(pack_dir(pid, required=False))
            new_answers = {k: v for k, v in (setup_answers or {}).items() if v}
            sd = SAVES / plan["save"]
            resuming = mode in ("resume", "candidate_resume")
            if resuming:
                prior, _ = safe_load_yaml(sd / "world" / "setup.yaml")
                setup_values = resolve_answers(setup_items, {**(prior if isinstance(prior, dict) else {}), **new_answers}, strict=False)
                if mode == "resume":
                    set_current(world_name)
                apply_setup(sd, setup_items, setup_values)
                marker_meta, _ = safe_load_yaml(self._seed_marker(sd)) if self._seed_marker(sd).exists() else ({}, None)
                protagonist_pref = protagonist_pref or str((marker_meta or {}).get("protagonist_pref") or "")
            elif mode == "candidate_new":
                old = SAVES / world_name
                prior, _ = safe_load_yaml(old / "world" / "setup.yaml")
                setup_values = resolve_answers(setup_items, {**(prior if isinstance(prior, dict) else {}), **new_answers}, strict=False)
                sd = create_save(plan["save"], pid, make_current=False)
                candidates.mark(sd, world_name)
                apply_setup(sd, setup_items, setup_values)
                overlay_tree(old / "mods", sd / "mods")
                candidates.set_world_name(sd, world_name)
            else:
                setup_values = resolve_answers(setup_items, setup_answers, strict=setup_answers is not None)
                sd = create_save(world_name, pid, setup_answers)

            timeline.ensure_ignored(sd)
            seed_marker = self._seed_marker(sd)
            seed_marker.write_text(
                yaml.safe_dump(
                    {"state": "generating", "started_at": datetime.datetime.now().isoformat(timespec="seconds"), "protagonist_pref": protagonist_pref},
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            setup_notes = describe_for_seed(setup_items, setup_values)

            seed_req = (
                f"请按【创世向导 · 阶段二：孕育世界母本种子】为新存档 `saves/{sd.name}/`（世界包 `{pid}`）生成完整的第 0 天开局世界母本：\n"
                f"- 玩家指定主角与开局偏好：{protagonist_pref or '按世界包 protagonist.md 默认模板塑造有血有肉的主角'}\n"
                + (f"- 开局设置（已写入存档，不可更改；标明交给你决定的由你补全）：\n{setup_notes}\n" if setup_notes else "")
                + ("- 这个存档里可能有上次没生成完的内容：先看已有的文件，补齐缺的，不要重复创建；`mods/` 是玩家的定制，不要改。\n" if resuming else "")
                + (f"- 这是为「{world_name}」重新生成一个全新的世界，只在这个目录里写。目录名是临时的，不要写进任何设定；`mods/` 是玩家的定制，不要改。\n" if plan["replaces"] else "")
                + "- 世界早在主角之前就存在：开局这一年对世界只是普通的一年。除非世界包明确这样设定，否则不要把它写成刚改元、刚开国或纪元元年，也不要把世界级大变故都安排在开局这一年。\n"
                + "- `style_pack.md` 范例区只用来校准文笔，里面的人物、物件和情节不是这个世界的内容，开场要自己设计。\n"
                + "必须完成以下分层切片落盘动作并通过存档强校验：\n"
                "1. 填写 `world/canon.md`（玩家亲定事实）与 `world/lore.md`（世界常识）\n"
                "2. **严格按主题分层切片（单文件 ≤ 12KB，切勿糊成单个大文件）**：\n"
                "   - 在 `world/design/` 下拆分为 `01-核心循环与分卷规划.md`、`02-序章开场剧本.md` 等小切片；\n"
                "   - 在 `world/secrets/` 下拆分为 `01-深层惊天真相.md`、`02-暗流布局者.md`、`03-主角暗线与分卷揭示计划.md` 等小切片（切勿在返回简报中剧透！）\n"
                "3. 在 `world/factions/` 创建至少 2~3 个核心势力卡（`fac_*.md`，带推进时钟 `clock`）\n"
                "4. 在 `world/locations/` 创建初始详细地点（`detail: full`）与至少 2 个连通邻近地点（配置双向 `neighbors`）\n"
                "5. 在 `world/npcs/` 创建至少 2~3 名开局关键 NPC（含至少 1 名带 `questline` 的 A 级人物，严格符合 `world/rules.yaml` 的阶位与属性范围）；人物卡的台词示例写这个人平时真会说的普通话，不写金句、顶真和故作高深的话\n"
                "6. 初始化 `player/sheet.md`（严格符合 `world/rules.yaml` 范围）、`player/journal.md`（仅写主角开局已知日常）、`world/events/timeline.yaml`（至少 3 条 `planned` 事件）、`world/hooks.md`、`world/director.md`\n"
                "7. 维护 `world/stage.md`：填好 `## 当前阶段`、`## 台前必读`（≤ 6 个当前阶段核心文件）与 `## 用来埋伏笔的秘密`（仅列本阶段需埋伏笔的 1~2 个秘密切片），并更新 `world/now.md` 的初始 `location` 与 `present` 在场人物\n"
                "8. 世界包若带 `backstage.md`，严格按其中的开局生成清单与幕后规则执行；世界包模板里已有的文件（如 `world/canon.md`、`design/`、`secrets/` 的骨架）在原文件上填写，不改名、不另起一份。\n"
                "9. 按 world-sim 的“剧情规划”一节建好 `world/design/` 下的分卷规划（每卷主冲突、终结节点和节拍）、排期总表、节奏备忘和剧情规划（当前卷节拍 + 开局起的前三章）。\n"
                "10. 运行存档合规校验确保 0 错误！最后返回一份不含剧透的创世母本简报。"
            )
            try:
                brief = self.run_world_sim(
                    sd,
                    {"task_type": "开局种子生成", "day_range": "第0天", "request": seed_req},
                    cfg,
                    on_status=on_status,
                )
                if on_status:
                    on_status({"type": "status", "phase": "seed_export", "label": "正在整理世界…", "world_sim": False})

                apply_setup(sd, setup_items, setup_values)
                sync_sliced_indexes(sd)
                rep, ents = validate(sd)
                now, _ = read_frontmatter(sd / "world" / "now.md")
                start = ents.get((now or {}).get("location"))
                if not start or start[0] != "locations":
                    rep.err("world/now.md", "开局种子缺少已生成的起始地点")
            except Exception as exc:
                self._mark_seed_failed(sd, exc)
                raise
            if rep.errors:
                self._mark_seed_failed(sd, "；".join(rep.errors))
            seed_file = None
            if not rep.errors:
                try:
                    git_turn_checkpoint(sd, "孕育世界母本种子", prefix="[seed]")
                    if not plan["replaces"]:
                        seed_file = export_seed(sd)
                    seed_marker.unlink()
                except Exception as exc:
                    self._mark_seed_failed(sd, exc)
                    raise

            counts = {
                "npcs": sum(1 for v in ents.values() if v[0] == "npcs"),
                "locations": sum(1 for v in ents.values() if v[0] == "locations"),
                "factions": sum(1 for v in ents.values() if v[0] == "factions"),
            }
            return {
                "ok": len(rep.errors) == 0,
                "stage": "seed",
                "save_name": sd.name,
                "replaces": plan["replaces"],
                "pack_id": pid,
                "counts": counts,
                "seed_bundle_path": seed_file.name if seed_file else "",
                "validation_errors": rep.errors,
                "summary": brief,
                "trace_id": current_trace_id(),
                "hud": self.get_hud_state(None if plan["replaces"] else sd.name),
            }

        elif stage == "customize_ui":
            sd = save_dir(payload.get("save_name"), required=False)
            pid = (payload.get("pack_id") or active_pack_id(sd) or "").strip()
            ui_pref = (payload.get("ui_pref") or "").strip()
            target_pdir = pack_dir(pid, required=False)
            if not target_pdir:
                raise ValueError("未找到目标世界包")

            if on_status:
                on_status({"type": "status", "phase": "ui_design", "label": "正在为你定制世界配色、字体、对话框样式与四周像素画…", "world_sim": False})

            is_api = is_api_like_driver(driver_cfg)
            sys_prompt = build_system_prompt(sd, role="ui", pack_id=pid, protocol="api" if is_api else "tools")
            ui_prompt = (
                f"请执行【创世向导 · 阶段四：按世界观生成定制化世界 UI、四周像素画与剧情动态氛围预设】（严格遵循 skills/ui-designer/SKILL.md）。\n"
                f"- 目标世界包：`packs/{pid}/`\n"
                f"- 宏观骨架（顶部信息栏、右侧状态栏、右侧调教窗、底部输入台）位置和结构固定，不改 web/static/。字段规范都在系统提示的 ui-designer 里，不要读 engine/ 和 web/ 的源码，也不要写脚本预览。\n"
                f"- 玩家的界面、对话框、像素画与动态氛围偏好：{ui_pref or '请根据该世界包的氛围、规则与角色设定，自动定制沉浸式主题配色、正文字体排版、对话框样式、四周像素风格风物画，并预设随剧情自动切换的多情境氛围（日常探索 / 冲突危机 / 潜行悬疑 / 休整日常）'}\n\n"
                f"请先读取 `pack:pack.yaml` 与 `pack:rules.yaml`（若有存档也可读 `save:player/sheet.md` 对齐自定义字段），然后一次性写入精美的：\n"
                f"1. `theme_patch`：包含 `css_vars`（全套主题色）、`typography`（正文字体/标题字体/字号行高）、`dialogue_box`（从 cyber-chamfer / classic-rpg / parchment-scroll / gothic-ornate / terminal-crt / minimal-glass 中选最契合的对话框样式 + `gutter_x` 留白宽度 + `corner_ornament` 角标 + `quote_style` 台词高亮）、`skyline`（顶部天际线色板）、`pixel_frame`（四周留白像素画：`scene` 场景 + `weather` 像素粒子 + 自定义 `palette` 色表 + 3~4 个分布在 left-top / right-top / left-bottom / right-bottom 的等宽字符矩阵像素精灵 `sprites`），以及 **`mood_presets`（至少包含 `default` 常态、`combat` 冲突危机、`mystery` 悬疑潜行、`rest` 安全休整 4 套随剧情动态切换的主题色调与像素天气预设）**！\n"
                f"2. `ui_schema_patch`：包含 `tagline`、`hud_tabs`（键限 scene/sheet/persona/milestones）、带多样化合法 `widget`（仅限 `badge` / `bar` / `fraction` / `meter` / `tags` / `kv`）的 `world_status` 与 `player_status`、按需显隐的 `character_sheet` 及 `journal_sections`。\n"
                f"完成后简短介绍你设计的配色、对话框、四周像素画风物，以及剧情转入不同情境时的动态变装效果。"
            )
            tctx = {"pack_id": pid, "applied_actions": [], "ui_updated": False}
            if not sd or active_pack_id(sd) != pid or ui_mode(load_setup(target_pdir), load_theme(None, pid)) == "ask":
                tctx["write_scope"] = "pack"
            elif active_pack_id(sd) == pid:
                if is_api:
                    ui_prompt += "\n\n注意：这个世界包已有默认界面，直接调用 `update_world_ui` 提交即可（引擎会自动写入本存档的 `mods/` 定制层，不会改动原世界包）。"
                else:
                    ui_prompt += f"\n\n注意：这个世界包已有界面，改动只属于当前存档，写入 `saves/{sd.name}/mods/theme.json` 和 `mods/pack.yaml`，不要改世界包。"
            ui_save = None if tctx.get("write_scope") == "pack" else sd
            ui_root = ui_save / "mods" if ui_save else target_pdir
            roots = {ui_root, target_pdir}
            if sd:
                roots.add(sd / "mods")
            before_files = {p: p.read_bytes() if p.is_file() else None
                            for root in roots for p in (root / "theme.json", root / "pack.yaml")}
            before_ui = (load_theme(ui_save, pid), load_ui_schema(ui_save, pid))
            try:
                if driver_cfg.get("type") == "cli":
                    res = run_cli_turn(driver_cfg, ui_prompt, role="tuner", on_status=on_status, system_prompt=sys_prompt)
                elif driver_cfg.get("type") == "harness":
                    res = run_harness_turn(
                        driver_cfg, sys_prompt, ui_prompt, role="tuner", on_status=on_status, sd=sd, tuner_context=tctx, max_steps=10
                    )
                else:
                    res = run_openai_compat_loop(
                        driver_cfg, sys_prompt, [{"role": "user", "content": ui_prompt}], sd,
                        role="tuner", tuner_context=tctx, max_steps=10, stream=bool(on_status), on_status=on_status
                    )
                if any(p.parent != ui_root and (p.read_bytes() if p.is_file() else None) != raw
                       for p, raw in before_files.items()):
                    raise ValueError("界面定制写入了错误的位置")
                errors = mods_layer.check_mods(ui_save)[0] if ui_save else doctor_pack(pid)["errors"]
                if errors:
                    raise ValueError("界面定制未通过校验：" + "；".join(errors))
                if before_ui == (load_theme(ui_save, pid), load_ui_schema(ui_save, pid)):
                    raise ValueError("没有检测到有效的界面更新，请重试")
            except Exception:
                for p, raw in before_files.items():
                    current = p.read_bytes() if p.is_file() else None
                    if current != raw:
                        if raw is None:
                            p.unlink()
                        else:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_bytes(raw)
                raise

            if sd and not parse_story_History(sd, limit=1):
                ui_schema = load_ui_schema(sd, pid)
                jpath = sd / "player" / "journal.md"
                if jpath.exists():
                    existing_j = read_text(jpath)
                    stripped_real = re.sub(r"(?m)^#.*$|^<!--.*?-->$|_（暂无记录）_", "", existing_j).strip()
                    if len(stripped_real) < 12:
                        jpath.write_text(build_initial_journal(ui_schema), encoding="utf-8")
                else:
                    jpath.write_text(build_initial_journal(ui_schema), encoding="utf-8")

            return {
                "ok": True,
                "stage": "customize_ui",
                "pack_id": pid,
                "ui_updated": True,
                "summary": res.get("text", ""),
                "trace_id": current_trace_id(),
                "hud": self.get_hud_state(sd.name if sd else None),
            }

        raise ValueError(f"未知的创世引导阶段：{stage}")

    def _run_genesis_job(self, stage, payload, on_status=None):
        self._refuse_while_job()
        plan = self._plan_seed_target(payload) if stage == "seed" else None
        if plan:
            self._begin_job(stage, plan["save"], world=plan["world"], replaces=plan["replaces"], pack=plan["pack"])
        else:
            self._begin_job(stage, pack=(payload.get("pack_id") or "").strip())

        def track(event):
            job = self.__dict__.get("_job")
            if job and isinstance(event, dict) and event.get("label"):
                job["phase"] = event["label"]
            on_status(event)

        try:
            result = self._run_genesis_core(stage, payload, on_status=track if on_status else None, plan=plan)
        except StreamCancelled as exc:
            self._end_job(ok=False, error=str(exc), cancelled=True)
            raise
        except Exception as exc:
            self._end_job(ok=False, error=str(exc))
            raise
        errors = result.get("validation_errors") or (result.get("doctor") or {}).get("errors") or []
        self._end_job(
            ok=bool(result.get("ok", True)),
            error="；".join(errors)[:300],
            result={
                "save_name": result.get("save_name", ""),
                "replaces": result.get("replaces", ""),
                "pack_id": result.get("pack_id", ""),
                "counts": result.get("counts") or {},
                "errors": errors,
                "summary": result.get("summary", ""),
            },
        )
        return result

    def run_genesis_stage(self, stage, payload):
        """执行开局向导的单步动作（同步版）。"""
        with trace_scope("genesis", stage=stage, pack=payload.get("pack_id", "")):
            return self._run_genesis_job(stage, payload, on_status=None)

    def stream_genesis_stage(self, stage, payload):
        """执行创世向导工作流的单步动作（流式状态推送版）。"""
        return self._stream_via_worker(
            "genesis",
            {"stage": stage, "pack": payload.get("pack_id", "")},
            lambda emit: self._run_genesis_job(stage, payload, on_status=emit),
            detach=True,
        )

    # ==================== 主线跑团回合调度 ====================

    @staticmethod
    def _auto_roll_summary_if_needed(sd, window_turns=14):
        """旧接口保留：现在由 _schedule_chapter 写章节摘要。"""
        return None

    def get_story_page(self, save_name=None, before=None, limit=STORY_PAGE):
        sd = save_dir(save_name)
        limit = max(1, min(int(limit or STORY_PAGE), 100))
        return {"turns": parse_story_History(sd, limit=limit, before=int(before) if before else None), "total": story_log.count_turns(sd)}

    def _schedule_chapter(self, sd):
        total = story_log.count_turns(sd)
        span = chapter_log.next_chapter_range(sd, total)
        if not span:
            return None
        worker = threading.Thread(target=self._write_chapter, args=(sd, span), daemon=True)
        self._chapter_thread = worker
        worker.start()
        return worker

    def _write_chapter(self, sd, span):
        with self._get_save_lock(sd):
            total = story_log.count_turns(sd)
            if chapter_log.next_chapter_range(sd, total) != span:
                return
            turns = [t for t in story_log.all_turns(sd) if span[0] <= t["index"] <= span[1]]
            title = body = ""
            try:
                cfg = load_config()
                driver = {**front_driver_for_turn(cfg), "save_name": sd.name}
                title, body = chapter_log.split_summary(self._plain_text_call(driver, chapter_log.summary_prompt(turns), sd))
            except Exception as exc:
                log_error("chapter.summary_err", exc=exc, save=sd.name)
            if not body:
                title, body = chapter_log.mechanical_chapter(turns)
            chapter_log.append_chapter(sd, turns, title, body)
            log_event("chapter.written", save=sd.name, start=span[0], end=span[1], chars=len(body))

    @staticmethod
    def _plain_text_call(driver, prompt, sd, role="summary"):
        dtype = driver.get("type")
        if dtype == "cli":
            return run_cli_turn(driver, prompt, role=role, tools="").get("text") or ""
        if dtype == "harness":
            return run_harness_turn(driver, "", prompt, role=role, sd=sd, tools=[], max_steps=1).get("text") or ""
        return run_openai_compat_loop(driver, "", [{"role": "user", "content": prompt}], sd, role=role, max_steps=1, tools=[]).get("text") or ""

    @staticmethod
    def _recent_turns(sd, cfg):
        context_cfg = cfg.get("context") or {}
        full_n = int(context_cfg.get("recent_full_turns") or 3)
        total = story_log.count_turns(sd)
        covered = chapter_log.covered_until(sd, total)
        window = max(int(context_cfg.get("sliding_window_turns") or 8), min(total - covered, chapter_log.CHAPTER_TURNS + full_n + 3))
        turns = parse_story_History(sd, limit=window)
        older, recent = turns[:-full_n], turns[-full_n:]
        digest = []
        for t in older:
            lines = [line.strip() for line in (t.get("story") or "").splitlines() if line.strip()]
            digest.append(f"- {(t.get('input') or '继续').strip()[:40]} → {lines[-1][:60] if lines else ''}")
        blocks, messages = [], []
        for t in recent:
            said = (t.get("input") or "").strip() or "（继续推进剧情）"
            story = (t.get("story") or "").strip()
            messages += [{"role": "user", "content": said}, {"role": "assistant", "content": story}]
            blocks.append(f"**玩家：** {said}\n\n{story}")
        older_md = ("## 更早的回合（一句话）\n" + "\n".join(digest) + "\n\n") if digest else ""
        recent_md = ("## 最近回合原文（紧接最后一幕往下写，不要重复）\n\n" + "\n\n---\n\n".join(blocks) + "\n\n---\n\n") if blocks else ""
        if digest and messages:
            messages[0] = {"role": "user", "content": f"{older_md}{messages[0]['content']}"}
        return older_md + recent_md, messages

    def _handle_export_command(self, sd, raw_args: str, stream=False, on_delta=None) -> dict:
        args_str = (raw_args or "").strip()
        low = args_str.lower()
        if low.startswith("import "):
            src_arg = args_str.split(None, 1)[1].strip().strip("\"'")
            src_path = Path(src_arg) if Path(src_arg).is_absolute() else (ROOT / src_arg)
            res = import_bundle(src_path)
            msg = f"✓ 已导入 `{res.get('id')}`（类型：{res.get('type', 'pack')}）。"
        elif low == "pack" or low.startswith("pack "):
            parts = args_str.split(None, 1)
            pid = (parts[1].strip() if len(parts) > 1 else "") or active_pack_id(sd)
            if not pid:
                raise ValueError("当前没有可导出的世界包")
            out = export_pack(pid)
            msg = f"✓ 已导出纯净题材规则包：`exports/{out.name}`"
        elif low == "seed" or low.startswith("seed "):
            out = export_seed(sd)
            msg = f"✓ 已静默导出脱敏世界母本种子（第0天开局态）：`exports/{out.name}`"
        else:
            fmt = "md" if ("md" in low or "markdown" in low) else "html"
            out = export_novel(sd, fmt=fmt)
            msg = f"✓ 已导出跑团小说成品：`exports/{out.name}`"
        if stream and on_delta:
            on_delta(msg)
        return {
            "ok": True,
            "command": "export",
            "archived": False,
            "story": msg,
            "trace_id": current_trace_id(),
            "hud": self.get_hud_state(sd.name),
        }

    def _handle_snapshot_command(self, sd, user_input: str, cfg: dict, stream=False, on_status=None, on_delta=None) -> dict:
        timeline.commit_pending(sd)
        pre_head = timeline.head(sd)
        try:
            meta, body = read_frontmatter(sd / "world" / "now.md")
            meta = meta or {}
            day = int(meta.get("day", 0))
            vol = int(meta.get("volume", 1))
            snap_rel = f"log/snapshots/第{vol}卷-第{day}天.md"
            self.run_world_sim(
                sd,
                {
                    "task_type": "世界快照",
                    "day_range": f"第{day}天",
                    "request": f"生成第 {vol} 卷第 {day} 天完整世界快照并写入 `{snap_rel}`（含各方暗流与未揭示秘密的当前进展，仅存档留档，不在返回简报剧透）。",
                },
                cfg,
                on_status=on_status,
            )
            meta, body = read_frontmatter(sd / "world" / "now.md")
            meta = meta or {}
            meta["last_snapshot_day"] = day
            write_frontmatter(sd / "world" / "now.md", meta, body)
            git_turn_checkpoint(sd, user_input, prefix="[snapshot]")
        except Exception:
            timeline.restore(sd, pre_head)
            raise
        msg = f"✓ 已在幕后生成并封存全局世界快照：`{snap_rel}`（未揭示暗线不在此处展示）。"
        if stream and on_delta:
            on_delta(msg)
        return {
            "ok": True,
            "command": "snapshot",
            "archived": False,
            "story": msg,
            "trace_id": current_trace_id(),
            "hud": self.get_hud_state(sd.name),
        }

    def _run_turn_core(self, user_input, save_name=None, stream=False, on_status=None, on_delta=None, on_step_reset=None):
        self._refuse_while_job()
        sd = save_dir(save_name)
        with self._get_save_lock(sd):
            cfg = load_config()
            cmd_name, expanded_input = self.expand_slash_command(user_input, sd)
            raw_cmd_args = (user_input or "").strip().partition(" ")[2].strip() if cmd_name else ""

            if cmd_name == "retry":
                return self._swipe_core(
                    feedback=raw_cmd_args,
                    save_name=sd.name,
                    stream=stream,
                    on_status=on_status,
                    on_delta=on_delta,
                    on_step_reset=on_step_reset,
                )

            if cmd_name == "undo":
                res = self.undo_last_turn(save_name=sd.name)
                if stream and on_delta:
                    on_delta(res["message"])
                return {**res, "command": "undo", "archived": False, "story": res["message"]}

            if cmd_name == "export":
                return self._handle_export_command(sd, raw_cmd_args, stream=stream, on_delta=on_delta)

            if cmd_name == "snapshot":
                return self._handle_snapshot_command(sd, user_input, cfg, stream=stream, on_status=on_status, on_delta=on_delta)

            if on_status:
                on_status({"type": "status", "phase": "context", "label": "正在感知当前场景脉络…", "world_sim": False})

            front_cfg = {**front_driver_for_turn(cfg), "save_name": sd.name}
            is_oai_sdk = front_cfg.get("type") == "harness" and str(front_cfg.get("harness_mode") or "").strip().lower() == "openai_agents_sdk"
            story_turn = self._command_counts_as_turn(sd, cmd_name) and cmd_name not in ("tick", "newworld")
            read_only_cmd = cmd_name in ("recap", "foresight")
            inline_turn = story_turn and (front_cfg.get("type") in ("cli", "api") or is_oai_sdk)

            # 先把回合外的改动存档并记下快照锚点：回合前幕后推演与台前写正文纳入同一原子事务
            synced_before = timeline.commit_pending(sd)
            pre_head = timeline.head(sd)
            pre_turn_count = len(parse_story_History(sd, limit=ALL_TURNS))
            pending_before = pending_turn_path(sd).read_bytes() if pending_turn_path(sd).exists() else None
            log_event(
                "turn.anchor", save=sd.name, head=pre_head, turns=pre_turn_count, pre_turn_sync=synced_before,
                driver=driver_log_meta(front_cfg), inline=inline_turn, stream=stream,
            )
            snapshot_saved = None
            # 本轮出了什么没写进存档的问题，都要明白告诉玩家，不能只记在日志里
            turn_notices = []
            front_guard = ToolRunGuard()

            try:
                sim_brief = self._run_due_sim(sd, cfg, on_status, pre_head=pre_head, notices=turn_notices) if story_turn else ""
                brief_md = f"## 幕后刚结算完（不要直接告诉玩家）\n{sim_brief}\n\n" if sim_brief else ""

                ctx = assemble_turn_context(sd, user_input, cfg)
                ctx_md = format_context_markdown(ctx)
                protocol = "inline" if inline_turn else ("api" if is_api_like_driver(front_cfg) else "tools")
                sys_prompt = build_system_prompt(sd, role="front", protocol=protocol)

                recent_turns_md, history_msgs = self._recent_turns(sd, cfg)
                log_event(
                    "turn.context", save=sd.name, pack=ctx.get("pack"), day=ctx.get("day"),
                    cmd=cmd_name, protocol=protocol, clean_action=ctx.get("clean_action"),
                    inline_notes=ctx.get("inline_director_notes"),
                    assembled_files=list(ctx["files"]), context_chars=len(ctx_md),
                    system_chars=len(sys_prompt), recent_chars=len(recent_turns_md),
                    sim_brief_chars=len(sim_brief),
                )

                effective_input = expanded_input if cmd_name else (ctx.get("clean_action") or expanded_input)
                full_user_msg = f"{brief_md}{ctx_md}\n\n---\n\n## 【玩家本轮输入】\n{effective_input}"
                history_msgs.append({"role": "user", "content": full_user_msg})

                changes_filter = ChangesStreamFilter(on_delta) if inline_turn and on_delta else None

                def _step_reset():
                    if changes_filter:
                        changes_filter.reset()
                    if on_step_reset:
                        on_step_reset()

                cli_prompt = (
                    f"当前存档：saves/{sd.name}/（只读写这个存档和它的世界包，不要打开其他存档或世界包）\n\n"
                    f"{recent_turns_md}"
                    f"{brief_md}"
                    f"{ctx_md}\n\n---\n\n"
                    f"## 玩家本轮行动\n{effective_input}"
                    f"{INLINE_TAIL if inline_turn else ''}"
                )

                light_cli = inline_turn or read_only_cmd
                if front_cfg.get("type") == "cli":
                    pre_cli_snap = _snapshot_save_tree(sd) if light_cli else None
                    full_cli_tools = "Read,Write,Edit,Glob,Grep,Bash,Agent" if _is_claude_cli(front_cfg) else "Read,Write,Edit,Glob,Grep,Bash"
                    res = run_cli_turn(
                        front_cfg,
                        cli_prompt,
                        role="front",
                        stream=stream,
                        on_status=on_status,
                        on_delta=changes_filter.feed if changes_filter else on_delta,
                        system_prompt=sys_prompt,
                        tools="Read,Grep,Glob" if light_cli else full_cli_tools,
                        replace_system=light_cli,
                        needs_project=not light_cli,
                        on_step_reset=_step_reset,
                    )
                    if pre_cli_snap is not None:
                        reverted_rels = _restore_save_tree_if_changed(sd, pre_cli_snap)
                        if reverted_rels:
                            log_event(
                                "turn.cli_readonly_reverted",
                                lvl="WARN",
                                save=sd.name,
                                cli_tool=front_cfg.get("cli_tool"),
                                files=reverted_rels,
                            )
                elif front_cfg.get("type") == "harness":
                    harness_prompt = (
                        cli_prompt
                        if is_oai_sdk
                        else (
                            f"当前存档：saves/{sd.name}/\n\n"
                            f"{recent_turns_md}"
                            f"{brief_md}"
                            f"{ctx_md}\n\n---\n\n"
                            f"## 【玩家本轮行动/指令】\n{effective_input}"
                        )
                    )
                    res = run_harness_turn(
                        front_cfg,
                        sys_prompt,
                        harness_prompt,
                        role="front",
                        stream=stream,
                        on_status=on_status,
                        on_delta=changes_filter.feed if (is_oai_sdk and changes_filter) else on_delta,
                        sd=sd,
                        tools=(FRONT_INLINE_TOOLS_OPENAI if (inline_turn or read_only_cmd) else None) if is_oai_sdk else None,
                        world_sim_callback=lambda sim_args: self.run_world_sim(sd, sim_args, cfg, on_status=on_status),
                        max_steps=10,
                        tool_guard=front_guard,
                    )
                else:
                    api_tools = FRONT_INLINE_TOOLS_OPENAI if (inline_turn or read_only_cmd) else None
                    res = run_openai_compat_loop(
                        front_cfg,
                        sys_prompt,
                        [{"role": "user", "content": cli_prompt}] if inline_turn else history_msgs,
                        sd,
                        role="front",
                        world_sim_callback=lambda sim_args: self.run_world_sim(sd, sim_args, cfg, on_status=on_status),
                        stream=stream,
                        on_status=on_status,
                        on_delta=changes_filter.feed if changes_filter else on_delta,
                        on_step_reset=_step_reset,
                        tools=api_tools,
                        tool_guard=front_guard,
                    )
                if changes_filter:
                    changes_filter.finish()

                story_text = (res.get("text") or "").strip()
                if front_guard.unresolved_write_errors:
                    log_event(
                        "turn.tool_write_failed", lvl="ERR", save=sd.name, command=cmd_name,
                        writes_ok=front_guard.writes_ok, errors=front_guard.unresolved_write_errors,
                    )
                    turn_notices.append(f"这次要改的内容没有写进存档：{front_guard.unresolved_write_errors[0][:160]}")
                if inline_turn:
                    missing_block = not has_changes_block(story_text)
                    story_text, inline_ops, inline_error = split_changes(story_text)
                    if story_text and missing_block and not timeline.story_changed_since(sd, pre_head):
                        inline_ops, inline_error, found = self._recover_changes(front_cfg, sys_prompt, cli_prompt, story_text, sd, on_status)
                        missing_block = not found
                    if story_text:
                        self._apply_inline_changes(sd, inline_ops, inline_error, missing_block=missing_block)
                elif story_turn and has_changes_block(story_text):
                    story_text, inline_ops, inline_error = split_changes(story_text)
                    if story_text and (inline_ops or inline_error):
                        self._apply_inline_changes(sd, inline_ops, inline_error, missing_block=False)
                counts_as_turn = self._command_counts_as_turn(sd, cmd_name)
                if counts_as_turn:
                    agent_archived = timeline.story_changed_since(sd, pre_head)
                    if agent_archived is None:
                        agent_archived = len(parse_story_History(sd, limit=ALL_TURNS)) > pre_turn_count
                    if not story_text and not agent_archived:
                        raise ValueError("模型没有返回本轮正文，本轮未生效")
                    if on_status:
                        on_status({"type": "status", "phase": "archiving", "label": "正在归档本轮记忆…", "world_sim": False})
                    display_input = (ctx.get("clean_action") or user_input) if cmd_name != "newworld" else OPENING_TURN_LABEL
                    if not agent_archived:
                        archived_rel = archive_story_turn(sd, display_input, story_text)
                        log_event("turn.archived", save=sd.name, archived_to=archived_rel, story_chars=len(story_text))
                    snapshot_saved = timeline.fold_turn(sd, pre_head, display_input)
                elif cmd_name == "save":
                    sync_sliced_indexes(sd)
                    rep, _ = validate(sd)
                    if rep.errors:
                        raise ValueError("存档校验未通过：" + "；".join(rep.errors))
                    git_turn_checkpoint(sd, user_input, prefix="[save]")
                elif cmd_name == "gm":
                    git_turn_checkpoint(sd, user_input, prefix="[gm]")
            except Exception as exc:
                _restore_pending_bytes(sd, pending_before)
                restored = timeline.restore(sd, pre_head)
                log_event(
                    "turn.atomic_rollback", lvl="WARN" if restored else "ERR", save=sd.name,
                    ok=restored, target_head=pre_head, error_type=type(exc).__name__,
                )
                raise

            pending = read_pending_turn(sd) if counts_as_turn else {}
            state_synced = not (pending.get("missing_changes") or pending.get("errors"))
            try:
                post_head = timeline.head(sd) if counts_as_turn else None
            except OSError:
                post_head = None  # 诊断读取失败不能把已归档的回合变成失败
            log_event(
                "turn.outcome", lvl="WARN" if not state_synced or (pre_head and snapshot_saved is False) else "INFO",
                save=sd.name, command=cmd_name, archived=counts_as_turn, state_synced=state_synced,
                missing_changes=bool(pending.get("missing_changes")), state_errors=len(pending.get("errors") or []),
                queued_sim_requests=len(pending.get("sims") or []), snapshot_saved=snapshot_saved,
                head_before=pre_head, head_after=post_head,
            )
        if counts_as_turn:
            self._schedule_chapter(sd)
        if turn_notices and not counts_as_turn and story_text:
            # 命令回复（如 /gm）不归档，提示直接接在回复后面，免得模型嘴上说改好了、玩家信以为真
            story_text = story_text + "\n\n" + "\n".join(f"> ⚠ 引擎提示：{n}" for n in turn_notices)
        return {
            "ok": True,
            "command": cmd_name,
            "archived": counts_as_turn,
            "state_synced": state_synced,
            "message": "\n".join(
                ([] if state_synced else ["正文已归档，但本轮世界状态没有同步。可重演本轮。"]) + turn_notices
            ),
            "story": story_text,
            "inline_notes_applied": ctx.get("inline_director_notes") or [],
            "model": res.get("model", ""),
            "trace_id": current_trace_id(),
            "hud": self.get_hud_state(sd.name),
        }

    RECOVER_ATTEMPTS = 2

    @classmethod
    def _recover_changes(cls, front_cfg, sys_prompt, turn_prompt, story_text, sd, on_status=None):
        """正文后面漏了变更块：带着刚写的正文再问，只要变更块。返回（操作，解析错误，拿到没有）。

        补问本身也可能落空（只想不写、写坏 JSON、接口报错），最多问 RECOVER_ATTEMPTS 次，免得一整轮状态白白丢掉。
        """
        if on_status:
            on_status({"type": "status", "phase": "state_sync", "label": "正在铭刻世界状态…", "world_sim": False})
        last_err = ""
        for attempt in range(1, cls.RECOVER_ATTEMPTS + 1):
            ops, err, found = cls._recover_changes_once(front_cfg, sys_prompt, turn_prompt, story_text, sd, attempt)
            if found and not err:
                return ops, "", True
            last_err = err or last_err
        log_event("turn.changes_missing_after_retry", lvl="WARN", save=sd.name, attempts=cls.RECOVER_ATTEMPTS, err=last_err[:300])
        return [], last_err, bool(last_err)

    @staticmethod
    def _recover_changes_once(front_cfg, sys_prompt, turn_prompt, story_text, sd, attempt=1):
        """问一次。返回（操作，解析错误，拿到没有）。"""
        try:
            if front_cfg.get("type") == "cli":
                pre_rec_snap = _snapshot_save_tree(sd)
                res = run_cli_turn(
                    front_cfg,
                    f"{turn_prompt}\n\n## 你刚写好的这一轮正文\n\n{story_text}\n\n---\n\n{RECOVER_CHANGES_ASK}",
                    role="front",
                    system_prompt=sys_prompt,
                    tools="Read,Grep,Glob",
                    replace_system=True,
                )
                _restore_save_tree_if_changed(sd, pre_rec_snap)
            elif front_cfg.get("type") == "harness":
                res = run_harness_turn(
                    front_cfg,
                    sys_prompt,
                    f"{turn_prompt}\n\n## 你刚写好的这一轮正文\n\n{story_text}\n\n---\n\n{RECOVER_CHANGES_ASK}",
                    role="front",
                    sd=sd,
                    tools=[],
                    max_steps=1,
                )
            else:
                res = run_openai_compat_loop(
                    front_cfg,
                    sys_prompt,
                    [
                        {"role": "user", "content": turn_prompt},
                        {"role": "assistant", "content": story_text},
                        {"role": "user", "content": RECOVER_CHANGES_ASK},
                    ],
                    sd,
                    role="front",
                    max_steps=1,
                    tools=[],
                )
            text = (res.get("text") or "").strip()
        except Exception as exc:
            log_error("turn.recover_changes_err", exc=exc, save=sd.name, attempt=attempt)
            return [], "", False
        if has_changes_block(text):
            _, ops, err = split_changes(text)
            log_event("turn.changes_recovered", lvl="WARN" if err else "INFO", save=sd.name, ops=len(ops), err=err, attempt=attempt)
            return ops, err, True
        bare = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        if bare.startswith("["):
            _, ops, err = split_changes(f"x\n<state_changes>\n{bare}\n</state_changes>")
            if not err:
                log_event("turn.changes_recovered", save=sd.name, ops=len(ops), bare=True, attempt=attempt)
                return ops, "", True
        log_event("turn.recover_changes_empty", lvl="WARN", save=sd.name, reply=text[:300], attempt=attempt)
        return [], "", False

    @staticmethod
    def _apply_inline_changes(sd, ops, parse_error="", missing_block=False):
        sims = [op for op in ops if op.get("op") == "request_sim"]
        real_ops = [op for op in ops if op.get("op") != "request_sim"]
        errors = [parse_error] if parse_error else []
        applied = 0
        if real_ops:
            result = apply_operations(sd, real_ops, commit_checkpoint=False)
            errors += result.get("errors") or []
            applied = result.get("done", 0)
        path = pending_turn_path(sd)
        prior = read_pending_turn(sd)
        prior_sims = prior.get("sims") or []
        pending = {k: v for k, v in prior.items() if k not in ("errors", "ops", "sims", "missing_changes")}
        if errors:
            pending.update(errors=errors, ops=real_ops)
            log_event("turn.changes_failed", lvl="WARN", save=sd.name, errors=errors)
        if missing_block:
            pending["missing_changes"] = True
            log_event("turn.changes_missing", lvl="WARN", save=sd.name)
        if sims or prior_sims:
            pending["sims"] = prior_sims + sims
        if pending:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        elif path.exists():
            path.unlink()
        log_event(
            "turn.state_sync", lvl="WARN" if errors or missing_block else "INFO", save=sd.name,
            requested=len(real_ops), applied=applied, sim_requests=len(sims),
            errors=len(errors), missing_changes=missing_block, parse_error=bool(parse_error),
        )

    def _run_due_sim(self, sd, cfg, on_status, pre_head=None, notices=None):
        """到了该推演的时候先让幕后跑完。若玩家中途取消则抛给外层整轮回滚；若仅幕后报错则回滚半截脏文件并保留请求。

        notices：失败时往里加一句给玩家看的说明（本轮照常进行，但世界变化没有结算）。
        """
        pending_path = pending_turn_path(sd)
        pending_before = None
        started = False
        stage = "prepare"
        rejection_reason = ""
        due_count = 0
        unresolved = 0
        try:
            meta, body = read_frontmatter(sd / "world" / "now.md")
            meta = meta or {}
            if meta.get("frozen"):
                return ""
            day, last = int(meta.get("day", 0)), int(meta.get("last_tick_day", 0))
            tick_days = int(load_world(sd).get("tick_days", 7) or 7)
            active = [e for e in load_events(sd) if e.get("status") in ACTIVE]
            due = {e["id"]: e for e in active if last < e["day"] <= day}
            due_count = len(due)
            pending = read_pending_turn(sd)
            sims = pending.get("sims") or []
            turns_now = story_log.count_turns(sd)
            plan_short = plot.needs_planning(sd) and turns_now - int(pending.get("plan_tried_turn", -99)) >= PLAN_RETRY_TURNS
            if not (sims or due_count or plan_short or day - last >= tick_days):
                return ""
            log_event(
                "world_sim.scheduled", save=sd.name, day=day, last_tick_day=last, tick_interval=tick_days,
                due_events=due_count, overdue_past_tick=sum(e["day"] <= last for e in active),
                requests=len(sims), plan_short=plan_short,
            )
            if due and not pre_head:
                log_event("world_sim.settlement", lvl="ERR", save=sd.name, status="rejected",
                          reason="no_rollback_anchor", due_at_start=due_count)
                return ""
            chronicle = sd / "world" / "events" / "chronicle.md"
            chronicle_before = set(read_text(chronicle).splitlines()) if due and chronicle.is_file() else set()
            pending_before = pending_path.read_bytes() if pending_path.exists() else None
            started = True
            wanted = "；".join(f"{s.get('task', '造物')}：{s.get('detail', '')}" for s in sims)
            if plan_short:
                added = plot.ensure_templates(sd)
                wanted = (wanted + "；" if wanted else "") + (
                    "剧情规划不够了：按“剧情规划”一节补齐分卷规划、排期总表、节奏备忘和接下来几章"
                    + (f"（引擎刚放好了骨架：{'、'.join(added)}，在上面填写）" if added else "")
                    + "；剧情规划的二级、三级标题照那一节写"
                )
                pending["plan_tried_turn"] = turns_now
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            wanted = wanted or "结算这段时间的到期事件和势力动向，更新前兆风声和导演笔记。"
            if due:
                wanted += (
                    "\n结算本次范围内的到期事件并落盘。事件若移出时间线，须在新增的编年史记录中注明原事件 ID。"
                    "`needs_scene: true` 或参与者包含 `player` 的事件留在时间线，交给台前演出。"
                )
            stage = "model"
            brief = self.run_world_sim(
                sd,
                {
                    "task_type": "推演" if day > last else ("规划剧情" if plan_short and not sims else "造物"),
                    "day_range": f"第{last + 1}天 ~ 第{day}天" if day > last else "当前",
                    "request": wanted,
                },
                cfg,
                on_status=on_status,
                settle_by_engine=True,
            )
            stage = "acceptance"
            if due and not (sd / "world" / "events" / "timeline.yaml").is_file():
                rejection_reason = "missing_timeline"
                raise ValueError("幕后结算后缺少事件时间线")
            waiting_for_player = {}
            remaining = {e["id"]: e for e in load_events(sd)}
            # 包括推演中新增或改期至本次结算范围内的事件。
            unresolved = sum(
                eid not in due and e["status"] in ACTIVE and last < e["day"] <= day
                for eid, e in remaining.items()
            )
            if due or unresolved:
                report, _ = validate(sd)
                if report.errors:
                    rejection_reason = "invalid_save"
                    raise ValueError(f"幕后结算存档校验失败（{len(report.errors)} 项）")
            if due:
                added_chronicle = set()
                if due.keys() - remaining.keys() and chronicle.is_file():
                    added_chronicle = set(read_text(chronicle).splitlines()) - chronicle_before
                for eid, original in due.items():
                    event = remaining.get(eid)
                    needs_scene = original.get("needs_scene") is True or "player" in (original.get("participants") or [])
                    if needs_scene:
                        if event is not None and event["status"] in ACTIVE and event["day"] == original["day"]:
                            waiting_for_player[eid] = event
                        else:
                            unresolved += 1
                        continue
                    if event is None:
                        if not any(re.search(rf"(?<![\w-]){re.escape(eid)}(?![\w-])", line) for line in added_chronicle):
                            unresolved += 1
                    elif event["status"] in ACTIVE and event["day"] <= day:
                        unresolved += 1
            if unresolved:
                rejection_reason = "unresolved_events"
                raise ValueError(f"幕后结算未通过验收（{unresolved} 条到期事件未落实）")

            meta, body = read_frontmatter(sd / "world" / "now.md")
            if due and meta.get("day") != day:
                rejection_reason = "world_day_changed"
                raise ValueError("幕后结算擅自改动了当前游戏日")
            stage = "persist"
            tick_day = min(e["day"] for e in waiting_for_player.values()) - 1 if waiting_for_player else day
            meta["last_tick_day"] = tick_day
            write_frontmatter(sd / "world" / "now.md", meta, body)
            pending = read_pending_turn(sd)
            pending.pop("sims", None)
            if pending:
                pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            elif pending_path.exists():
                pending_path.unlink()
            log_event(
                "world_sim.tick_recorded", save=sd.name, previous_tick_day=last, tick_day=tick_day,
                cleared_requests=len(sims), due_at_start=due_count, due_still_active=len(waiting_for_player),
            )
            log_event(
                "world_sim.settlement", save=sd.name, status="awaiting_player" if waiting_for_player else "accepted",
                tick_day=tick_day, due_at_start=due_count, awaiting_player=len(waiting_for_player),
            )
            if waiting_for_player:
                handoff = "\n".join(
                    f"- `{eid}` {e.get('title') or e.get('name') or ''}：{e.get('summary') or ''}"
                    for eid, e in waiting_for_player.items()
                )
                brief = f"{brief or ''}\n\n### 台前待演事件（仅用于主持，不提前揭示）\n{handoff}"
            return brief
        except StreamCancelled:
            log_event("world_sim.settlement", lvl="WARN", save=sd.name, status="cancelled", stage=stage,
                      due_at_start=due_count)
            raise
        except Exception as exc:
            restored = bool(pre_head and timeline.restore(sd, pre_head)) if started else None
            if started:
                _restore_pending_bytes(sd, pending_before)
            log_event(
                "world_sim.settlement", lvl="ERR" if restored is False else "WARN", save=sd.name,
                status="rejected" if stage == "acceptance" else "failed", stage=stage,
                reason=rejection_reason or type(exc).__name__, rollback_ok=restored,
                due_at_start=due_count, unresolved=unresolved,
            )
            log_error("turn.pre_sim_err", exc=exc, save=sd.name, rollback_ok=restored, target_head=pre_head)
            if restored is False:
                raise
            if started and notices is not None:
                notices.append(f"幕后推演这次没有完成（{str(exc)[:120]}），这段时间的世界变化没有结算，下一轮会再试。")
            return ""

    def run_turn(self, user_input, save_name=None):
        """执行一轮玩家输入或斜杠命令（同步版）。"""
        sd = save_dir(save_name)
        with trace_scope("turn", save=sd.name, pack=active_pack_id(sd)):
            return self._run_turn_core(user_input, save_name=sd.name, stream=False)

    def stream_turn(self, user_input, save_name=None):
        """执行一轮玩家输入或斜杠命令（SSE 无剧透状态指示 + 流式打字机版）。"""
        sd = save_dir(save_name)
        return self._stream_via_worker(
            "turn",
            {"save": sd.name, "pack": active_pack_id(sd)},
            lambda emit: self._run_turn_core(
                user_input,
                save_name=sd.name,
                stream=True,
                on_status=emit,
                on_delta=lambda txt: emit({"type": "delta", "text": txt}),
                on_step_reset=lambda: emit({"type": "step_reset"}),
            ),
        )

    def _swipe_core(self, feedback="", save_name=None, stream=False, on_status=None, on_delta=None, on_step_reset=None):
        """撤回最近一轮并重演。重演失败或被中止时，把存档原样恢复到撤回之前，玩家不会丢失上一轮。"""
        self._refuse_while_job()
        sd = save_dir(save_name)
        with self._get_save_lock(sd):
            turns = parse_story_History(sd, limit=1)
            if not turns:
                raise ValueError("当前存档尚无已归档的故事回合可撤销")
            last_input = turns[-1]["input"]

            if on_status:
                on_status({"type": "status", "phase": "reverting", "label": "↩️ 正在回溯上一轮时间线快照…", "world_sim": False})

            undone = timeline.undo_last_turn(sd)
            notice = timeline.describe_undo(undone)
            log_event("swipe.reverted", save=sd.name, via_git=undone["via_git"], last_input=last_input, has_feedback=bool(feedback))

            rerun_input = OPENING_RERUN_INPUT if last_input == OPENING_TURN_LABEL else last_input
            if feedback:
                rerun_input = f"{rerun_input}（场外：{feedback}）".strip()
            if not rerun_input:
                return self._undo_result(sd, last_input, notice)

            try:
                res = self._run_turn_core(
                    rerun_input,
                    save_name=sd.name,
                    stream=stream,
                    on_status=on_status,
                    on_delta=on_delta,
                    on_step_reset=on_step_reset,
                )
            except Exception as exc:
                restored = timeline.restore(sd, undone["pre_head"])
                log_error("swipe.rerun_err", exc=exc, save=sd.name, restored=restored)
                if isinstance(exc, StreamCancelled):
                    raise
                raise RuntimeError(f"重演失败，上一轮已原样恢复：{exc}") from exc
            if notice:
                res["message"] = notice
            return res

    def _undo_result(self, sd, last_input, notice=""):
        message = "已撤回上一轮"
        return {
            "ok": True,
            "reverted_only": True,
            "last_input": "" if last_input == OPENING_TURN_LABEL else last_input,
            "message": f"{message}（{notice}）" if notice else message,
            "trace_id": current_trace_id(),
            "hud": self.get_hud_state(sd.name),
        }

    def undo_last_turn(self, save_name=None):
        """只撤回最近一轮，不重演。可连续调用，逐轮往回退。"""
        self._refuse_while_job()
        sd = save_dir(save_name)
        with self._get_save_lock(sd):
            turns = parse_story_History(sd, limit=1)
            if not turns:
                raise ValueError("当前存档尚无已归档的故事回合可撤销")
            undone = timeline.undo_last_turn(sd)
            return self._undo_result(sd, turns[-1]["input"], timeline.describe_undo(undone))

    def swipe_last_turn(self, feedback="", save_name=None):
        """一键撤销上一轮并可选重演（同步版）。"""
        sd = save_dir(save_name)
        with trace_scope("swipe", save=sd.name, pack=active_pack_id(sd)):
            return self._swipe_core(feedback=feedback, save_name=sd.name, stream=False)

    def stream_swipe_turn(self, feedback="", save_name=None):
        """一键撤销上一轮并流式重演（SSE 无剧透状态指示 + 流式打字机版）。"""
        sd = save_dir(save_name)
        return self._stream_via_worker(
            "swipe",
            {"save": sd.name, "pack": active_pack_id(sd)},
            lambda emit: self._swipe_core(
                feedback=feedback,
                save_name=sd.name,
                stream=True,
                on_status=emit,
                on_delta=lambda txt: emit({"type": "delta", "text": txt}),
                on_step_reset=lambda: emit({"type": "step_reset"}),
            ),
        )

    def edit_latest_story(self, new_story_text, save_name=None):
        """原地修改最近一轮的故事正文（Inline Edit）。"""
        self._refuse_while_job()
        sd = save_dir(save_name)
        turns = parse_story_History(sd, limit=2)
        if not turns:
            raise ValueError("当前还没有可编辑的故事记录")
        last = turns[-1]
        p = sd / last["file"]
        raw = read_text(p)
        old_story = last["story"]
        if old_story not in raw:
            raise ValueError("未能在档案中定位原段落")
        updated = raw.replace(old_story, new_story_text.strip(), 1)
        p.write_text(updated, encoding="utf-8")
        git_turn_checkpoint(sd, "原地润色正文", prefix="[polish]")
        return {"ok": True, "hud": self.get_hud_state(sd.name)}

    def add_style_example(self, excerpt, liked=True, reason="", save_name=None):
        """Web 端划词与免调AI快速笔记：写入当前存档 mods/style_pack.md 或世界包 style_pack.md 并登记 playtest.md。"""
        self._refuse_while_job()
        sd = save_dir(save_name, required=False)
        pid = active_pack_id(sd)
        pdir = pack_dir(pid, required=False)

        if sd and sd.is_dir() and pdir and pdir.is_dir():
            style_path = sd / "mods" / "style_pack.md"
            target_label = f"saves/{sd.name}/mods/style_pack.md"
        elif pdir and pdir.is_dir():
            style_path = pdir / "style_pack.md"
            target_label = f"{pid}/style_pack.md"
        elif sd and sd.is_dir():
            style_path = sd / "world" / "style_notes.md"
            target_label = f"saves/{sd.name}/world/style_notes.md"
        else:
            style_path = ROOT / "engine" / "docs" / "style_notes.md"
            target_label = "engine/docs/style_notes.md"

        text = read_text(style_path) if style_path.exists() else "# 题材文风补丁\n\n## 二、 范例区\n\n### 👍 喜欢的\n\n### 👎 不喜欢的\n"

        quoted = "\n".join("> " + l for l in excerpt.strip().splitlines())
        note = reason.strip() or ("节奏与细节恰当" if liked else "文风或叙事需规避")
        block = f"\n**{note}**：\n{quoted}\n"

        target_header = "### 👍 喜欢的" if liked else "### 👎 不喜欢的"
        if target_header in text:
            idx = text.index(target_header) + len(target_header)
            text = text[:idx] + "\n" + block + text[idx:]
        else:
            text = text.rstrip() + f"\n\n{target_header}\n{block}\n"
        style_path.parent.mkdir(parents=True, exist_ok=True)
        style_path.write_text(text, encoding="utf-8")

        pt_path = ROOT / "engine" / "docs" / "playtest.md"
        if pt_path.exists():
            today = datetime.date.today().isoformat()
            kind = "正例校准" if liked else "反例纠偏"
            row = f"| {today} | {pid or '无包'} / {sd.name if sd else '-'} | [{kind}] {note} | 已自动写入 `{target_label}` 范例区 | 已固化 |\n"
            with open(pt_path, "a", encoding="utf-8") as fh:
                fh.write(row)

        return {"ok": True, "pack": pid or "fallback", "target": target_header, "file": target_label}
