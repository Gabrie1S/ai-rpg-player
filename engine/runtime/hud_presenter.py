"""前端无剧透 HUD 状态、角色卡脱敏、手记分栏、自定义面板白名单与四步引导评估。"""
import json
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from common import (  # noqa: E402
    entity_name,
    load_config,
    read_frontmatter,
    safe_load_yaml,
    schema_map,
)
import mods as mods_layer  # noqa: E402
from statusline import format_widget_item  # noqa: E402
import story_log  # noqa: E402
from .driver_config import has_driver_ready  # noqa: E402

STORY_PAGE = 20
ALL_TURNS = 10**9


def parse_story_History(sd, limit=30, before=None):
    """从存档 log/story/ 读取已归档的故事回合，返回 [{index, file, volume, date_header, input, story}]。"""
    return story_log.story_turns(sd, limit=None if limit == ALL_TURNS else limit, before=before)


def parse_journal_sections(journal_text, ui_schema):
    """将 player/journal.md 按二级标题 `## ` 解析为结构化卡片分栏，并与世界包 ui_schema.journal_sections 对齐。"""
    raw = (journal_text or "").strip()
    schema_secs = (ui_schema or {}).get("journal_sections") or []
    parsed_map = {}
    order = []

    if raw:
        chunks = re.split(r"(?m)^##\s+", raw)
        for ch in chunks[1:]:
            lines = ch.splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            body_lines = [
                l for l in lines[1:]
                if not (l.strip().startswith("<!--") and l.strip().endswith("-->"))
            ]
            content = "\n".join(body_lines).strip()
            parsed_map[title] = content
            if title not in order:
                order.append(title)

    cards = []
    seen = set()
    for sec in schema_secs:
        title = sec.get("title") if isinstance(sec, dict) else str(sec)
        hint = sec.get("hint", "") if isinstance(sec, dict) else ""
        icon = sec.get("icon", "📓") if isinstance(sec, dict) else "📓"
        if not title:
            continue
        seen.add(title)
        cards.append({
            "title": title,
            "icon": icon,
            "hint": hint,
            "content": parsed_map.get(title, ""),
        })

    for title in order:
        if title not in seen:
            cards.append({
                "title": title,
                "icon": "📓",
                "hint": "",
                "content": parsed_map.get(title, ""),
            })
    return cards


def sanitize_player_sheet(sd, ui_schema=None):
    """提取玩家角色卡中主角本人可见的信息，并按世界包 ui_schema 生成自适应状态控件与角色卡模块数据。"""
    sheet_path = sd / "player" / "sheet.md"
    if not sheet_path.exists():
        return {}
    try:
        meta, body = read_frontmatter(sheet_path)
    except Exception:
        meta, body = {}, ""
    meta = meta or {}
    rules, _ = safe_load_yaml(sd / "world" / "rules.yaml")
    smap = schema_map(rules or {}, meta)
    rf = smap["rank_field"]
    af = smap["affinity_field"]
    apf = smap["aptitude_field"]
    ef = smap["engine_field"]
    cf = smap["currency_field"]

    shown_aff = meta.get(f"{af}_shown") or meta.get(f"{af}_active") or meta.get(af) or {}
    shown_apt = meta.get(f"{apf}_shown") or meta.get(apf) or {}

    rep_raw = meta.get("reputation") or {}
    rep_items = []
    if isinstance(rep_raw, dict):
        for fid, score in rep_raw.items():
            rep_items.append({
                "id": str(fid),
                "name": entity_name(sd, fid).split("（", 1)[0],
                "score": score if isinstance(score, (int, float)) else 0,
            })

    base = {
        "name": meta.get("name") or "未命名主角",
        "rank_label": rf,
        "rank": meta.get(rf) or "",
        "affinity_label": af,
        "affinity": shown_aff if isinstance(shown_aff, dict) else {},
        "aptitude_label": apf,
        "aptitude": shown_apt if isinstance(shown_apt, dict) else {},
        "engine_label": ef,
        "core_engine": meta.get(ef),
        "attributes": meta.get("attributes") or {},
        "skills": meta.get("skills") or [],
        "gear": meta.get("gear") or [],
        "abilities": meta.get("abilities") or [],
        "items": meta.get("items") or [],
        "currency_label": cf,
        "currency": meta.get(cf, 0),
        "reputation": rep_raw,
        "reputation_items": rep_items,
        "body": (body or "").strip(),
    }

    status_items = []
    for item in (ui_schema or {}).get("player_status") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key", "")
        field = item.get("field") or key
        wtype = item.get("widget", "kv")

        if key == "rank":
            raw_v = base["rank"] or "未定阶"
        elif key == "core_engine":
            eng = base["core_engine"]
            if isinstance(eng, dict) and eng.get("name"):
                raw_v = f"{eng['name']} ({eng.get('grade', '')}·{eng.get('orientation', '')})".strip(" ()·")
            else:
                raw_v = str(eng) if eng else "未搭载"
        elif key == "affinity":
            raw_v = " / ".join(f"{k}:{v}" for k, v in base["affinity"].items()) if base["affinity"] else "无"
        elif key == "aptitude":
            raw_v = " / ".join(f"{k}:{v}" for k, v in base["aptitude"].items()) if base["aptitude"] else "无"
        elif key == "currency":
            raw_v = base["currency"]
        else:
            raw_v = meta.get(field)
            if raw_v is None:
                raw_v = item.get("default")

        status_items.append(format_widget_item({**item, "widget": wtype}, raw_v, meta))

    base["status_items"] = status_items
    base["sheet_layout"] = (ui_schema or {}).get("character_sheet") or {}
    return base


def get_present_npcs_hud(sd, present_ids):
    """仅返回在场 NPC 的明面信息（id、姓名、阶位、一句话明面身份 brief、当前动向），绝不返回秘密与认知。"""
    rules, _ = safe_load_yaml(sd / "world" / "rules.yaml")
    smap = schema_map(rules or {})
    rf = smap["rank_field"]
    out = []
    for pid in present_ids or []:
        p = sd / "world" / "npcs" / f"{pid}.md"
        if not p.exists():
            continue
        try:
            m, _ = read_frontmatter(p)
            if m:
                out.append({
                    "id": pid,
                    "name": m.get("name", pid),
                    "rank": m.get(rf) or m.get("rank") or "",
                    "brief": m.get("brief", ""),
                    "mood": m.get("mood", ""),
                    "activity": m.get("activity", ""),
                })
        except Exception:
            pass
    return out


def build_onboarding_state(packs, saves_list, active_pid, cfg=None, theme=None):
    """启动时自动评估四阶段引导进度：①系统自检与驱动连接 → ②配置/拖入世界包 → ③孕育世界种子 → ④创建定制化UI与像素画。"""
    cfg = cfg or load_config()
    driver_ok = has_driver_ready(cfg)
    driver_verified = driver_ok
    ready_packs = [p for p in packs if p["status"] == "ready"]
    unparsed_packs = [p for p in packs if p["status"] in ("unparsed", "nested")]
    has_pack_save = any(s["pack"] == active_pid for s in saves_list) if active_pid else bool(saves_list)
    has_custom_ui = bool(isinstance(theme, dict) and (theme.get("pixel_frame") or theme.get("dialogue_box")))

    needs_onboarding = (not driver_ok) or (len(ready_packs) == 0) or (len(saves_list) == 0)

    if not driver_ok:
        rec_step = 1
    elif len(ready_packs) == 0:
        rec_step = 2
    elif not has_pack_save:
        rec_step = 3
    else:
        rec_step = 4

    return {
        "needs_onboarding": needs_onboarding,
        "total_packs_count": len(packs),
        "ready_packs_count": len(ready_packs),
        "unparsed_packs": [
            {"id": p["id"], "name": p["name"], "status": p["status"], "nested_subpath": p.get("nested_subpath")}
            for p in unparsed_packs
        ],
        "recommended_step": rec_step,
        "driver_ready": driver_ok,
        "driver_verified": driver_verified,
        "has_pack_save": has_pack_save,
        "has_custom_ui": has_custom_ui,
    }


def build_panel_state(sd, ui_schema, status_payload, now_meta, journal_text, persona_text, milestones_text):
    """自定义面板能拿到的全部数据。这里是白名单：只放玩家本来就能看到的内容。"""
    player = sanitize_player_sheet(sd, ui_schema)
    public_fields = ("name", "rank", "affinity", "aptitude", "core_engine", "attributes", "skills", "gear", "abilities", "items", "currency", "reputation_items", "status_items")
    state = (status_payload or {}).get("state") or {}
    rules, _ = safe_load_yaml(sd / "world" / "rules.yaml")
    rules = rules or {}
    turns = parse_story_History(sd, limit=3)
    panels = ui_schema.get("panels") or []
    return {
        "world": {
            "date": state.get("date", ""),
            "day": state.get("display_day", ""),
            "season": state.get("season", ""),
            "location": state.get("loc", ""),
            "mood": str(now_meta.get("ui_mood") or "default"),
        },
        "player": {k: player.get(k) for k in public_fields},
        "present_npcs": get_present_npcs_hud(sd, now_meta.get("present")),
        "journal": parse_journal_sections(journal_text, ui_schema),
        "persona": persona_text.strip(),
        "milestones": milestones_text.strip(),
        "recent_turns": [{"input": t.get("input", ""), "story": t.get("story", "")} for t in turns],
        "rules": {
            "ranks": [r.get("name") for r in rules.get("ranks") or [] if isinstance(r, dict)],
            "levels": rules.get("levels") or [],
            "attributes": rules.get("attributes") or [],
        },
        "data": {str(p.get("id")): mods_layer.panel_data(sd, p.get("id")) for p in panels if isinstance(p, dict)},
    }


def mask_config(cfg):
    """向前端返回配置时脱敏 api_key（前端仅在用户填入新 Key 时覆盖已保存的密钥）。"""
    out = json.loads(json.dumps(cfg or {}))
    out.pop("driver_verified_for", None)
    out["driver_verified"] = has_driver_ready(cfg)
    for dk in ("front_driver", "back_driver"):
        if isinstance(out.get(dk), dict):
            has_key = bool((out[dk].get("api_key") or "").strip())
            out[dk]["has_saved_key"] = has_key
            out[dk]["api_key"] = ""
    return out
