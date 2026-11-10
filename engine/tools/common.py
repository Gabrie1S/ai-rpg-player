"""公共工具：定位项目、即插即用题材包和存档，读写 YAML 头，通用历法换算与 UI/Schema 映射。"""
import copy
import json
import os
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # Windows 控制台输出中文
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import yaml
except ImportError:
    sys.exit("缺少 PyYAML：请先运行 pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parents[2]
SAVES = ROOT / "saves"
PACKS = ROOT / "packs"
EXPORTS = ROOT / "exports"
SKILLS = ROOT / "skills"
CONFIG_PATH = ROOT / "config.yaml"
CONFIG_LOCAL_PATH = ROOT / "config.local.yaml"
TEMPLATE_WORLD = ROOT / "engine" / "templates" / "world"

DEFAULT_SCHEMA_MAP = {
    "rank_field": "rank",
    "affinity_field": "affinity",
    "aptitude_field": "aptitude",
    "engine_field": "core_engine",
    "currency_field": "credits",
}

DEFAULT_UI_SCHEMA = {
    "tagline": "",
    "hud_tabs": {
        "scene": {"label": "日志卡", "icon": "📓"},
        "sheet": {"label": "角色状态", "icon": "👤"},
        "persona": {"label": "性格侧写", "icon": "🎭"},
        "milestones": {"label": "重要时刻", "icon": "🌟"},
    },
    "world_status": [
        {"key": "day", "label": "天数", "icon": "📅", "widget": "badge", "tone": "accent"},
        {"key": "date", "label": "日期", "icon": "🕒", "widget": "badge"},
        {"key": "loc", "label": "当前位置", "icon": "📍", "widget": "badge"},
    ],
    "player_status": [
        {"key": "rank", "label": "阶位", "icon": "⚡", "widget": "kv"},
        {"key": "core_engine", "label": "核心体系", "icon": "🧠", "widget": "kv"},
        {"key": "currency", "label": "资金", "icon": "💳", "widget": "kv"},
    ],
    "character_sheet": {
        "attributes_title": "基础属性",
        "sections": [
            {"id": "status_widgets", "title": "核心状态指标", "visible": True},
            {"id": "attributes", "title": "基础属性", "visible": True},
            {"id": "affinities", "title": "先天适性与潜能", "visible": True, "hide_empty": True},
            {"id": "abilities", "title": "特殊天赋与能力", "visible": True, "hide_empty": True},
            {"id": "skills", "title": "掌握技能", "visible": True, "hide_empty": True},
            {"id": "gear", "title": "随身装备", "visible": True, "hide_empty": False},
            {"id": "reputation", "title": "势力声望", "visible": True, "hide_empty": True},
            {"id": "inventory", "title": "背包物品与备注", "visible": True, "hide_empty": False},
        ],
    },
    "journal_sections": [
        {"id": "goals", "title": "当前目标", "icon": "🎯", "hint": "正在推进的事项与目标"},
        {"id": "people", "title": "认识的人", "icon": "🤝", "hint": "见过的人物与印象"},
        {"id": "rumors", "title": "听到的传闻", "icon": "📻", "hint": "打听到的风声与情报"},
        {"id": "secrets", "title": "追查的疑点", "icon": "🔍", "hint": "主角已知的秘密与未解线索"},
    ],
}

DEFAULT_NARRATIVE_PROMPT = (
    "【写作提醒】① 分段自然，用常见词，只写和情节有关的，一轮最多一个新名字； ② 因果写明白，主角有反应，人物首次出场交代姓名、身份、关系、外貌； ③ 台词像真人说话，不写金句和排好队的漂亮话； ④ 一轮演完整再停，只停在真正要你拿主意的地方；随口的小事按性格直接写完，不停在揭晓之前或半截反应上；不写升华总结，不列选项； ⑤ 公正裁决，鲁莽或越级就失败并付出代价。"
)

# 玩家没改过的模板也会被原样存进 config.yaml。默认值更新时把旧版本记在这里，否则新规则对已有配置永远不生效
RETIRED_NARRATIVE_PROMPTS = {
    "【写作提醒】① 分段自然，用常见词，只写和情节有关的，一轮最多一个新名字； ② 因果写明白，主角有反应，人物首次出场交代姓名、身份、关系、外貌； ③ 台词像真人说话，不写金句和排好队的漂亮话； ④ 推完正事再停，结尾留一件等你回应的事（一句问话、一个要求、一件刚发生的事），不停在发呆回想上；不写升华总结，不列选项； ⑤ 公正裁决，鲁莽或越级就失败并付出代价。",
    "【写作提醒】① 分段自然，用常见词，只写和情节有关的，一轮最多一个新名字； ② 因果写明白，主角有反应，人物首次出场交代姓名、身份、关系、外貌； ③ 台词像真人说话，不写金句和排好队的漂亮话； ④ 推完正事到重大抉择再停，不写升华总结，不列选项； ⑤ 公正裁决，鲁莽或越级就失败并付出代价。",
    "【通用叙事与文风自检（最高优先级）】 "
    "① 甩着看标准：正常分段（段间空行），日常物品与动作只用现代常见词（专有名词不限），每场景最多 1~2 处有效细节，每轮最多出 1 个新名字； "
    "② 可读性优先：反文艺腔留白，句子间因果连接词写明白，主角必有即时感受与反应，人物首次出场必须当场交代「姓名、身份、与主角关系、外貌印象」四要素； "
    "③ 台词像真人说话：人物开口是为了办眼前的事，不是给读者留金句。不写顶真、对仗、层层转折的漂亮句式（反例：“吃。吃完了才有力气问，问了我也不一定说。”），不让人物句句收得有味道、故作高深或抖机灵；想回避就用平常话岔开（如“先吃饭，凉了。”）；人物卡的台词示例只定口吻，不照抄原句； "
    "④ 节奏与边界：小事按主角性格（persona.md）直接写，推完正事到重大抉择再停；严禁在段尾写升华总结陈词，严禁列出 1/2/3 选项清单； "
    "⑤ 公正裁决：你不是讨好玩家的老好人，鲁莽或越级行动必须判败并给出真实代价。",
    "【通用叙事与文风自检（最高优先级）】 "
    "① 甩着看标准：正常分段（段间空行），日常物品与动作只用现代常见词（专有名词不限），每场景最多 1~2 处有效细节，每轮最多出 1 个新名字； "
    "② 可读性优先：反文艺腔留白，句子间因果连接词写明白，主角必有即时感受与反应，人物首次出场必须当场交代「姓名、身份、与主角关系、外貌印象」四要素； "
    "③ 节奏与边界：小事按主角性格（persona.md）直接写，推完正事到重大抉择再停；严禁在段尾写升华总结陈词，严禁列出 1/2/3 选项清单； "
    "④ 公正裁决：你不是讨好玩家的老好人，鲁莽或越级行动必须判败并给出真实代价。",
}


# 存档是玩家本地的独立仓库。新手电脑上往往没配置 Git 身份，不写这个提交会失败，撤回和重演就用不了
GIT_IDENTITY = ["-c", "user.name=AI-RPG-Player", "-c", "user.email=player@ai-rpg.local", "-c", "commit.gpgsign=false"]


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YAML_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")


def load_yaml(path):
    p = Path(path)
    if not p.exists():
        return None
    return yaml.load(read_text(p), Loader=_YAML_LOADER)


def safe_load_yaml(path):
    """安全加载 YAML，返回 (data, error_str)，绝不因外部拖入损坏文件崩溃。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None, None
    try:
        data = yaml.load(read_text(p), Loader=_YAML_LOADER)
        if data is not None and not isinstance(data, dict):
            return {}, f"{p.name} 顶层结构应为 YAML 字典"
        return data or {}, None
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else "YAML 语法错误"
        return {}, f"{p.name} 解析失败：{msg}"


def dump_yaml(data):
    return yaml.dump(data, Dumper=_YAML_DUMPER, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000).strip()


def _credential_scope(driver):
    base = str(driver.get("api_base") or "").strip().rstrip("/")
    return {"api_base": base} if base else {
        field: str(driver.get(field) or "") for field in ("type", "harness_mode", "api_format", "provider")
    }


def load_config():
    data = {}
    if CONFIG_PATH.exists():
        loaded, _ = safe_load_yaml(CONFIG_PATH)
        if isinstance(loaded, dict):
            data = loaded
    if CONFIG_LOCAL_PATH.exists():
        local_data, _ = safe_load_yaml(CONFIG_LOCAL_PATH)
        if isinstance(local_data, dict) and local_data:
            data = deep_merge(data, local_data)
    for dk in ("front_driver", "back_driver"):
        driver = data.get(dk)
        if isinstance(driver, dict):
            scope = driver.pop("_credential_scope", None)
            if scope is not None and scope != _credential_scope(driver):
                driver["api_key"] = ""
                driver["api_key_env"] = ""
    if data:
        prompt = data.get("narrative_prompt")
        if isinstance(prompt, dict) and str(prompt.get("authors_note_template") or "").strip() in RETIRED_NARRATIVE_PROMPTS:
            prompt["authors_note_template"] = DEFAULT_NARRATIVE_PROMPT
        return data
    return {"active_pack": "cyberpunk"}


def save_config(cfg):
    to_write = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    if to_write or CONFIG_LOCAL_PATH.exists():
        tracked_prev, _ = safe_load_yaml(CONFIG_PATH) if CONFIG_PATH.exists() else ({}, None)
        tracked_prev = tracked_prev if isinstance(tracked_prev, dict) else {}
        local_data, _ = safe_load_yaml(CONFIG_LOCAL_PATH)
        local_cfg = dict(local_data) if isinstance(local_data, dict) else {}

        for vkey in ("driver_verified", "driver_verified_at", "driver_verified_for"):
            if vkey in to_write:
                local_cfg[vkey] = to_write.pop(vkey)
            else:
                local_cfg.pop(vkey, None)

        if "active_pack" in local_cfg and "active_pack" in to_write:
            local_cfg["active_pack"] = to_write["active_pack"]
            if "active_pack" in tracked_prev:
                to_write["active_pack"] = tracked_prev["active_pack"]

        for dk in ("front_driver", "back_driver"):
            drv = to_write.get(dk)
            if isinstance(drv, dict):
                scope = _credential_scope(drv)
                local_drv = dict(local_cfg[dk]) if isinstance(local_cfg.get(dk), dict) else {}
                prev_drv = tracked_prev.get(dk) if isinstance(tracked_prev.get(dk), dict) else {}
                for nk in list(local_drv.keys()):
                    if nk not in ("api_key", "api_key_env", "_credential_scope") and nk in drv:
                        local_drv[nk] = drv[nk]
                        if nk in prev_drv:
                            drv[nk] = prev_drv[nk]
                for field in ("api_key", "api_key_env"):
                    if field in drv:
                        local_drv[field] = drv.pop(field) or ""
                if local_drv.get("api_key") or local_drv.get("api_key_env"):
                    local_drv["_credential_scope"] = scope
                else:
                    local_drv.pop("_credential_scope", None)
                if local_drv:
                    local_cfg[dk] = local_drv

        CONFIG_LOCAL_PATH.write_text(
            yaml.safe_dump(local_cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    CONFIG_PATH.write_text(
        yaml.safe_dump(to_write, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def current_save_name():
    env = os.environ.get("RPG_SAVE")
    if env:
        return env.strip()
    f = SAVES / "current.txt"
    if f.exists():
        name = f.read_text(encoding="utf-8", errors="replace").strip()
        if name:
            return name
    return None


def child_directory(root, name):
    """Resolve a single directory name without following links out of the collection."""
    if (not isinstance(name, str) or not name.strip() or name in (".", "..")
            or name.endswith((".", " ")) or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0].rstrip())):
        raise ValueError(f"不合法的目录名：{name!r}")
    dest = root / name
    if dest.resolve() != root.resolve() / name:
        raise ValueError(f"目录链接不允许作为包或存档：{name!r}")
    return dest


def save_dir(name=None, required=True):
    """返回存档目录。required=False 时找不到返回 None。"""
    name = name or current_save_name()
    if not name:
        if required:
            sys.exit("没有当前存档：先用 /newworld 或 newsave.py 新建，或在 saves/current.txt 写入存档名")
        return None
    try:
        d = child_directory(SAVES, name)
    except (ValueError, OSError, RuntimeError):
        if required:
            raise ValueError("不合法的存档目录") from None
        return None
    if not d.is_dir():
        if required:
            sys.exit(f"存档不存在：{d}")
        return None
    return d


def load_world(sd):
    if not sd:
        return {}
    data, _ = safe_load_yaml(Path(sd) / "world" / "world.yaml")
    return data or {}


def available_pack_ids():
    """扫描 packs/ 下所有独立文件夹（即插即用，删除文件夹自动卸载）。"""
    if not PACKS.is_dir():
        return []
    return [d.name for d in sorted(PACKS.iterdir()) if not d.name.startswith(".") and pack_exists(d.name)]


def pack_exists(pack_id):
    """检查指定世界包文件夹是否存在于 packs/ 目录中。"""
    return pack_dir(pack_id or "", required=False) is not None


def active_pack_id(sd=None):
    """获取当前生效的题材包 ID。若对应文件夹已被删除或 packs/ 为空，自动平滑降级，绝不崩溃。"""
    avail = available_pack_ids()
    if sd:
        w = load_world(sd)
        wp = str(w.get("pack") or "").strip()
        if wp:
            # 存档绑定了世界包就只用它。包被删除时返回空，由存档内置设定兜底，绝不换成别的世界
            return wp if wp in avail else ""
    cfg = load_config()
    cp = str(cfg.get("active_pack") or "").strip()
    if cp and cp in avail:
        return cp
    return avail[0] if avail else ""


def pack_dir(pack_id=None, sd=None, required=False):
    """定位题材包文件夹。如果文件夹被直接删除或 packs/ 为空，required=False 时平稳返回 None。"""
    pid = str(pack_id if pack_id is not None else active_pack_id(sd)).strip()
    try:
        d = child_directory(PACKS, pid)
    except (ValueError, OSError, RuntimeError):
        if required:
            sys.exit("未找到可用的世界观包：请先在 packs/ 放入世界包或使用创世向导创建")
        return None
    if not d.is_dir():
        if required:
            sys.exit(f"题材包文件夹不存在：{d}")
        return None
    return d


def schema_map(rules, meta=None):
    """从 rules.yaml 获取角色字段映射，支持不同题材自定义字段名。"""
    raw = (rules or {}).get("schema_mapping") or {}
    m = dict(DEFAULT_SCHEMA_MAP)
    if isinstance(raw, dict):
        m.update({k: v for k, v in raw.items() if v})
    if not raw and isinstance(meta, dict):
        if "street_tier" in meta and "rank" not in meta:
            m["rank_field"] = "street_tier"
        if "chrome_affinity" in meta and "affinity" not in meta:
            m["affinity_field"] = "chrome_affinity"
        if "os_core" in meta and "core_engine" not in meta:
            m["engine_field"] = "os_core"
        if "eddies" in meta and "credits" not in meta:
            m["currency_field"] = "eddies"
    return m


def mods_dir(sd):
    """存档定制层目录（saves/<存档>/mods/）。"""
    return Path(sd) / "mods" if sd else None


def deep_merge(base, over):
    """字典递归合并；带 id 的列表按 id 合并，其余列表整体替换。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = deep_merge(out[key], value) if key in out else copy.deepcopy(value)
        return out
    if isinstance(base, list) and isinstance(over, list) and all(isinstance(x, dict) and x.get("id") for x in base + over) and base + over:
        out = [copy.deepcopy(x) for x in base]
        positions = {str(x["id"]): i for i, x in enumerate(out)}
        for item in over:
            key = str(item["id"])
            if key in positions:
                out[positions[key]] = deep_merge(out[positions[key]], item)
            else:
                positions[key] = len(out)
                out.append(copy.deepcopy(item))
        return out
    return copy.deepcopy(over)


def _read_json_file(path):
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_theme(sd=None, pack_id=None):
    """当前生效的主题：世界包 theme.json，再叠加存档 mods/theme.json。"""
    pdir = pack_dir(pack_id, sd=sd, required=False)
    theme = _read_json_file(pdir / "theme.json") if pdir and (pdir / "theme.json").exists() else {}
    mod_path = mods_dir(sd) / "theme.json" if sd else None
    if mod_path and mod_path.exists():
        theme = deep_merge(theme, _read_json_file(mod_path))
    return theme


def _merge_custom_ui(ui, custom_ui):
    if custom_ui.get("tagline") is not None:
        ui["tagline"] = str(custom_ui.get("tagline") or "")
    if isinstance(custom_ui.get("hud_tabs"), dict):
        for tk, tv in custom_ui["hud_tabs"].items():
            if tk in ui["hud_tabs"] and isinstance(tv, dict):
                ui["hud_tabs"][tk].update(tv)
            elif tk in ui["hud_tabs"] and isinstance(tv, str):
                ui["hud_tabs"][tk]["label"] = tv
    for k in ("world_status", "player_status", "journal_sections"):
        if isinstance(custom_ui.get(k), list) and custom_ui.get(k):
            ui[k] = copy.deepcopy(custom_ui[k])
    if isinstance(custom_ui.get("quick_commands"), list):
        ui["quick_commands"] = copy.deepcopy(custom_ui["quick_commands"])
    if isinstance(custom_ui.get("panels"), list):
        ui["panels"] = deep_merge(ui.get("panels") or [], custom_ui["panels"]) if ui.get("panels") else copy.deepcopy(custom_ui["panels"])
    if isinstance(custom_ui.get("character_sheet"), dict):
        cs = custom_ui["character_sheet"]
        if cs.get("attributes_title"):
            ui["character_sheet"]["attributes_title"] = str(cs["attributes_title"])
        if isinstance(cs.get("sections"), list) and cs["sections"]:
            ui["character_sheet"]["sections"] = copy.deepcopy(cs["sections"])


def load_ui_schema(sd=None, pack_id=None):
    """当前生效的 UI 结构：内置默认 < 世界包 pack.yaml < 存档 mods/pack.yaml。包被删除时回退至默认。"""
    pdir = pack_dir(pack_id, sd=sd, required=False)
    ui = copy.deepcopy(DEFAULT_UI_SCHEMA)
    sources = [pdir / "pack.yaml"] if pdir else []
    if sd:
        sources.append(mods_dir(sd) / "pack.yaml")
    for path in sources:
        if not path.exists():
            continue
        meta, err = safe_load_yaml(path)
        custom_ui = meta.get("ui_schema") if not err and isinstance(meta, dict) else None
        if isinstance(custom_ui, dict):
            _merge_custom_ui(ui, custom_ui)
    if ui.get("panels"):
        ui["panels"] = [p for p in ui["panels"] if isinstance(p, dict) and p.get("enabled", True) is not False]
    return ui


_FM = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)$", re.S)


def read_frontmatter(path):
    """返回 (meta, body)。没有 YAML 头时 meta 为 None。YAML 出错会抛 yaml.YAMLError。"""
    text = read_text(path)
    m = _FM.match(text)
    if not m:
        return None, text
    meta = yaml.load(m.group(1), Loader=_YAML_LOADER) or {}
    return meta, m.group(2)


def write_frontmatter(path, meta, body):
    head = dump_yaml(meta)
    Path(path).write_text(f"---\n{head}\n---\n{body}", encoding="utf-8")


def day_to_date(day, calendar):
    """通用历法换算，支持自定义纪年前缀、起始年月日、每年月数与每月天数。"""
    cal = calendar or {}
    mpy = max(1, int(cal.get("months_per_year", 12) or 12))
    dpm = max(1, int(cal.get("days_per_month", 30) or 30))
    start = (int(cal.get("start_month", 1) or 1) - 1) * dpm + int(cal.get("start_day", 1) or 1) - 1
    total = start + int(day or 0)
    year = int(cal.get("start_year", 1) or 1) + total // (mpy * dpm)
    rem = total % (mpy * dpm)
    month, d = rem // dpm + 1, rem % dpm + 1
    era = str(cal.get("era", ""))
    month_names = cal.get("month_names")
    if isinstance(month_names, list) and len(month_names) >= month:
        m_str = str(month_names[month - 1])
    else:
        m_str = f"{month:02d}月"
    day_names = cal.get("day_names")
    if isinstance(day_names, list) and len(day_names) >= d:
        return f"{era}{year}年{m_str}{day_names[d - 1]}"
    return f"{era}{year}年{m_str}{d:02d}日"


def entity_name(sd, eid):
    """按 ID 找名字，找不到返回 ID 本身。"""
    if not eid:
        return "（无）"
    if eid == "player":
        return "玩家"
    folder = {"npc_": "npcs", "loc_": "locations", "fac_": "factions"}.get(str(eid)[:4])
    if folder and sd:
        p = Path(sd) / "world" / folder / f"{eid}.md"
        if p.exists():
            try:
                meta, _ = read_frontmatter(p)
                if meta and meta.get("name"):
                    return f"{meta['name']}（{eid}）"
            except Exception:
                pass
    return str(eid)
