"""通用动态像素风景状态栏（支持世界包 theme.json 自定义色板、都市霓虹与自然四季山川，状态组件完全跟随世界观包 ui_schema）。

只显示主角本来就知道的东西（由当前世界包 pack.yaml 的 ui_schema 决定显示哪些世界状态组件），绝不剧透。
同一个地点每次看到的建筑或山形一致（由地点 ID 哈希生成种子），霓虹灯、飞行器或云雪随游戏日动态演进。

  python engine/tools/statusline.py              终端 ANSI 渲染（4 行像素画 + 1 行状态信息）
  python engine/tools/statusline.py --rows 2     指定像素画行数（每行 2 个垂直像素）
  python engine/tools/statusline.py --json       输出 JSON 像素矩阵与自适应状态组件数据，供 Web App 渲染
"""
import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    load_theme,  # noqa: E402
    active_pack_id,
    day_to_date,
    load_ui_schema,
    load_world,
    pack_dir,
    read_frontmatter,
    read_text,
    safe_load_yaml,
    save_dir,
    schema_map,
)

RESET = "\x1b[0m"


def fg(c):
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"


def bg(c):
    return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


CYBER_PALETTES = {
    "megacity": dict(sky=((10, 12, 28), (28, 20, 52)), far=(24, 28, 54), near=(16, 18, 36),
                     ground=(18, 22, 38), neon1=(0, 240, 255), neon2=(255, 42, 133), accent=(255, 210, 60)),
    "slums": dict(sky=((18, 16, 24), (44, 30, 38)), far=(36, 32, 40), near=(26, 22, 28),
                  ground=(28, 26, 24), neon1=(255, 130, 40), neon2=(0, 210, 180), accent=(220, 70, 60)),
    "net_den": dict(sky=((4, 14, 18), (10, 36, 42)), far=(12, 48, 52), near=(8, 28, 32),
                    ground=(6, 20, 24), neon1=(0, 255, 156), neon2=(0, 190, 255), accent=(180, 255, 220)),
    "wasteland": dict(sky=((48, 32, 24), (96, 62, 40)), far=(72, 48, 34), near=(52, 36, 26),
                      ground=(84, 58, 38), neon1=(255, 180, 60), neon2=(220, 80, 40), accent=(255, 220, 130)),
}

CLASSIC_SEASONS = {
    "春": dict(sky=((150, 200, 235), (222, 236, 246)), far=(120, 160, 150), near=(96, 150, 104),
              ground=(118, 178, 92), tree=(84, 156, 78), accent=(242, 172, 192), sun=(255, 236, 160)),
    "夏": dict(sky=((84, 156, 228), (182, 216, 246)), far=(92, 140, 124), near=(60, 120, 74),
              ground=(72, 142, 62), tree=(38, 108, 50), accent=(250, 230, 120), sun=(255, 226, 110)),
    "秋": dict(sky=((226, 160, 112), (250, 222, 172)), far=(150, 116, 100), near=(138, 96, 64),
              ground=(196, 146, 66), tree=(204, 96, 44), accent=(232, 172, 56), sun=(250, 140, 70)),
    "冬": dict(sky=((160, 172, 192), (222, 226, 234)), far=(196, 202, 214), near=(150, 160, 170),
              ground=(234, 238, 244), tree=(64, 86, 76), accent=(250, 250, 252), sun=(244, 240, 226)),
}

SCENE_KEYWORDS = [
    ("net_den", "网黑客矩阵机房数据服务器潜入诊所实验室"),
    ("wasteland", "荒原废土垃圾场外围沙漠矿区"),
    ("slums", "锈船坞贫民窟下层黑市帮派巷街区"),
    ("megacity", "大厦巨企广场霓虹中枢商业塔高架核心"),
    ("water", "江河湖海港渡岛泽湾浦津"),
    ("sanctuary", "教堂神殿遗迹圣所修道院"),
    ("city", "城都京府坊市镇"),
    ("fortress", "堡垒要塞塔楼营地据点"),
    ("mountain", "山岭峰谷崖岳森林"),
    ("village", "村庄屯寨聚落"),
]

SCENE_LABELS = {
    "net_den": "矩阵/地下诊所",
    "wasteland": "外围荒原",
    "slums": "下城街区",
    "megacity": "企业高塔区",
    "water": "水域港口",
    "sanctuary": "圣所遗迹",
    "city": "城镇街区",
    "fortress": "据点要塞",
    "mountain": "山野险地",
    "village": "边陲聚落",
}


def detect_scene(name, tags, pack_id):
    text = " ".join(map(str, tags or [])) + " " + (name or "")
    for scene, chars in SCENE_KEYWORDS:
        if any(ch in text for ch in chars):
            return scene
    return "slums" if pack_id == "cyberpunk" else "village"


def load_pack_skyline_config(pack_id=None, sd=None):
    """读取世界包 theme.json 中的自定义 skyline 色板与几何风格（包不存在或未配置时平稳返回 None）。"""
    sky = load_theme(sd, pack_id).get("skyline")
    return sky if isinstance(sky, dict) else None


def _rgb(val, fallback):
    if isinstance(val, (list, tuple)) and len(val) == 3:
        try:
            return tuple(max(0, min(255, int(x))) for x in val)
        except Exception:
            pass
    return fallback


def paint_cyber(width, rows, scene, seed, day, custom_sky=None):
    H = rows * 2
    base_pal = CYBER_PALETTES.get(scene, CYBER_PALETTES["megacity"])
    if custom_sky:
        pal = dict(
            sky=(_rgb(custom_sky.get("sky_top"), base_pal["sky"][0]), _rgb(custom_sky.get("sky_bot"), base_pal["sky"][1])),
            far=_rgb(custom_sky.get("far"), base_pal["far"]),
            near=_rgb(custom_sky.get("near"), base_pal["near"]),
            ground=_rgb(custom_sky.get("ground"), base_pal["ground"]),
            neon1=_rgb(custom_sky.get("accent1"), base_pal["neon1"]),
            neon2=_rgb(custom_sky.get("accent2"), base_pal["neon2"]),
            accent=_rgb(custom_sky.get("highlight"), base_pal["accent"]),
        )
    else:
        pal = base_pal

    rng = random.Random(seed)
    horizon = max(2, H - max(1, H // 4))
    sky_top, sky_bot = pal["sky"]
    px = [[lerp(sky_top, sky_bot, y / max(1, horizon - 1)) for _ in range(width)] for y in range(H)]

    x = 0
    while x < width:
        bw = rng.randint(3, 6)
        bh = rng.randint(max(1, horizon // 3), max(2, horizon - 1))
        top = max(0, horizon - bh)
        for bx in range(x, min(width, x + bw)):
            for by in range(top, horizon):
                px[by][bx] = pal["far"]
                if (by - top) % 2 == 1 and (bx - x) in (1, bw - 2) and rng.random() < 0.45:
                    px[by][bx] = shade(pal["neon1"] if (bx + by) % 3 == 0 else pal["accent"], 0.65)
        x += bw + rng.randint(0, 2)

    x = 1
    while x < width - 2:
        bw = rng.randint(4, 7)
        bh = rng.randint(max(2, horizon // 2), horizon)
        top = max(0, horizon - bh)
        neon = pal["neon1"] if (x // 3) % 2 == 0 else pal["neon2"]
        for bx in range(x, min(width, x + bw)):
            for by in range(top, horizon):
                px[by][bx] = pal["near"]
                if by == top:
                    px[by][bx] = shade(neon, 0.85)
                elif (by + bx) % 2 == 0 and rng.random() < 0.35:
                    px[by][bx] = neon if rng.random() < 0.3 else pal["accent"]
        x += bw + rng.randint(2, 5)

    if horizon >= 3:
        fx = (seed + day * 5) % max(1, width - 2)
        fy = 1
        px[fy][fx] = pal["neon2"]
        if fx + 1 < width:
            px[fy][fx + 1] = pal["accent"]

    for y in range(horizon, H):
        for x in range(width):
            above = px[max(0, horizon - 1 - (y - horizon))][x]
            refl = lerp(pal["ground"], above, 0.35)
            px[y][x] = shade(refl, 0.9)
    return px


def paint_classic(width, rows, season, scene, seed, day, custom_sky=None):
    H = rows * 2
    base_pal = CLASSIC_SEASONS.get(season, CLASSIC_SEASONS["春"])
    if custom_sky:
        pal = dict(
            sky=(_rgb(custom_sky.get("sky_top"), base_pal["sky"][0]), _rgb(custom_sky.get("sky_bot"), base_pal["sky"][1])),
            far=_rgb(custom_sky.get("far"), base_pal["far"]),
            near=_rgb(custom_sky.get("near"), base_pal["near"]),
            ground=_rgb(custom_sky.get("ground"), base_pal["ground"]),
            accent=_rgb(custom_sky.get("accent1"), base_pal["accent"]),
            sun=_rgb(custom_sky.get("highlight"), base_pal["sun"]),
        )
    else:
        pal = base_pal

    rng = random.Random(seed)
    ground_rows = max(1, H // 3)
    horizon = H - ground_rows
    sky_top, sky_bot = pal["sky"]
    px = [[lerp(sky_top, sky_bot, y / max(1, horizon - 1)) for _ in range(width)] for y in range(H)]

    if horizon >= 3:
        sx = (seed + day * 3) % max(4, width - 4)
        px[1][sx] = pal["sun"]

    p1, p2 = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
    f1, f2 = rng.uniform(0.08, 0.16), rng.uniform(0.25, 0.4)
    for x in range(width):
        ridge = 1 - abs(math.sin(x * f1 + p1))
        h = (0.35 + 0.45 * ridge + 0.2 * math.sin(x * f2 + p2)) * horizon * 0.75
        top = max(0, min(horizon, horizon - int(round(h))))
        for y in range(top, horizon):
            px[y][x] = shade(pal["far"], 1.0 - 0.06 * (y - top))
    for y in range(horizon, H):
        for x in range(width):
            px[y][x] = shade(pal["ground"], 1.0 - 0.07 * (y - horizon))
    return px


def render_ansi(px):
    out = []
    for r in range(0, len(px), 2):
        top, bot = px[r], px[r + 1] if r + 1 < len(px) else px[r]
        out.append("".join(fg(t) + bg(b) + "▀" for t, b in zip(top, bot)) + RESET)
    return out


def format_widget_item(item_def, raw_val, container_meta=None):
    """将单个状态栏定义与原始数值组装为前端自适应控件所需的结构化对象。"""
    key = item_def.get("key", "")
    label = item_def.get("label", key)
    icon = item_def.get("icon", "")
    widget = item_def.get("widget", "badge")
    tone = item_def.get("tone", "normal")
    unit = item_def.get("unit", "")
    max_val = item_def.get("max")
    if item_def.get("max_field") and isinstance(container_meta, dict):
        mf = container_meta.get(item_def["max_field"])
        if isinstance(mf, (int, float)):
            max_val = mf

    if widget == "tags":
        if isinstance(raw_val, list):
            tags = [str(x) for x in raw_val if str(x).strip()]
        elif isinstance(raw_val, str) and raw_val.strip():
            tags = [s.strip() for s in raw_val.replace("、", ",").replace("/", ",").split(",") if s.strip()]
        else:
            tags = []
        return {
            "key": key,
            "label": label,
            "icon": icon,
            "widget": "tags",
            "tags": tags,
            "value": " · ".join(tags) if tags else "无",
        }

    if widget in ("bar", "fraction", "meter"):
        max_num = float(max_val) if isinstance(max_val, (int, float)) and max_val > 0 else 100.0
        num = None
        if isinstance(raw_val, (int, float)):
            num = float(raw_val)
        elif isinstance(raw_val, str) and "/" in raw_val:
            parts = raw_val.split("/", 1)
            try:
                num = float(parts[0].strip())
                max_num = float(parts[1].strip())
            except Exception:
                pass
        elif raw_val is not None:
            try:
                num = float(str(raw_val).rstrip("%").strip())
            except Exception:
                num = None

        if num is None:
            default_v = item_def.get("default", 0)
            num = float(default_v) if isinstance(default_v, (int, float)) else 0.0

        pct = max(0.0, min(100.0, round(num * 100.0 / max_num, 1))) if max_num > 0 else 0.0
        disp_num = int(num) if float(num).is_integer() else round(num, 1)
        disp_max = int(max_num) if float(max_num).is_integer() else round(max_num, 1)
        warn_at = float(item_def.get("warn_at", 60))
        danger_at = float(item_def.get("danger_at", 85))
        level = "danger" if pct >= danger_at else ("warn" if pct >= warn_at else "ok")
        disp_str = f"{disp_num}{unit}" if (widget == "bar" and unit == "%") else f"{disp_num} / {disp_max}{unit}"

        return {
            "key": key,
            "label": label,
            "icon": icon,
            "widget": widget,
            "color": item_def.get("color", "primary"),
            "current": disp_num,
            "max": disp_max,
            "percent": pct,
            "level": level,
            "unit": unit,
            "value": disp_str,
        }

    val_str = str(raw_val) if (raw_val is not None and raw_val != "") else str(item_def.get("default", ""))
    if val_str and (item_def.get("prefix") or item_def.get("suffix")):
        val_str = f"{item_def.get('prefix', '')}{val_str}{item_def.get('suffix', '')}"
    return {
        "key": key,
        "label": label,
        "icon": icon,
        "widget": widget,
        "tone": tone,
        "value": val_str,
    }


def resolve_world_status_items(ui_schema, base_state, now_meta, world_meta):
    """根据当前世界包 ui_schema.world_status 动态解析自适应世界状态栏组件列表。"""
    items = []
    for item in (ui_schema or {}).get("world_status") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key", "")
        if key in ("day", "display_day"):
            raw_val = f"第 {base_state['display_day']} 天"
        elif key == "date":
            raw_val = base_state["date"]
        elif key in ("loc", "location"):
            raw_val = base_state["loc"] or "未知地点"
        elif key == "scene":
            raw_val = SCENE_LABELS.get(base_state["scene"], base_state["scene"])
        elif key == "season":
            raw_val = base_state["season"]
        else:
            field = item.get("field") or key
            raw_val = now_meta.get(field)
            if raw_val is None:
                raw_val = world_meta.get(field)
            if raw_val is None:
                raw_val = item.get("default", "")

        built = format_widget_item({**item, "widget": item.get("widget", "badge")}, raw_val, now_meta)
        if built.get("value") != "" or built.get("widget") in ("bar", "fraction", "meter"):
            items.append(built)
    return items


def game_state(save_name=None):
    sd = save_dir(save_name, required=False)
    if not sd:
        return None
    try:
        now, _ = read_frontmatter(sd / "world" / "now.md")
    except Exception:
        now = {}
    now = now or {}
    day = int(now.get("day", 0) or 0)
    world = load_world(sd)
    raw_pack_id = str(world.get("pack") or active_pack_id(sd) or "")
    cal = world.get("calendar") or {}
    mpy = max(1, int(cal.get("months_per_year", 12) or 12))
    dpm = max(1, int(cal.get("days_per_month", 30) or 30))
    start = (int(cal.get("start_month", 1) or 1) - 1) * dpm + int(cal.get("start_day", 1) or 1) - 1
    month = ((start + day) % (mpy * dpm)) // dpm + 1
    season = "春夏秋冬"[min(3, (month - 1) * 4 // mpy)]

    loc_id = now.get("location")
    loc_name, tags = "", []
    if loc_id:
        p = sd / "world" / "locations" / f"{loc_id}.md"
        if p.exists():
            try:
                meta, _ = read_frontmatter(p)
                loc_name, tags = (meta or {}).get("name", ""), (meta or {}).get("tags", [])
            except Exception:
                loc_name = str(loc_id)
        else:
            loc_name = str(loc_id)

    rules, _ = safe_load_yaml(sd / "world" / "rules.yaml")
    smap = schema_map(rules or {})
    rf = smap["rank_field"]
    rank_val = ""
    player_name = ""
    try:
        sheet, _ = read_frontmatter(sd / "player" / "sheet.md")
        sheet = sheet or {}
        rank_val = sheet.get(rf) or sheet.get("rank") or ""
        player_name = sheet.get("name", "")
        if not loc_name and sheet.get("location"):
            loc_name = str(sheet.get("location"))
    except Exception:
        pass

    scene = detect_scene(loc_name, tags, raw_pack_id)
    ui_schema = load_ui_schema(sd, raw_pack_id)
    base = dict(
        save=sd.name,
        pack=raw_pack_id,
        genre=world.get("genre", ""),
        player_name=player_name,
        day=day,
        display_day=day + 1,
        date=day_to_date(day, cal),
        season=season,
        loc_id=loc_id or "",
        loc=loc_name,
        scene=scene,
        rank=rank_val,
        frozen=bool(now.get("frozen")),
    )
    base["world_status_items"] = resolve_world_status_items(ui_schema, base, now, world)
    return base


def info_line(st, model=""):
    dim, neon, ink, cyan = fg((140, 148, 165)), fg((0, 240, 255)), fg((228, 232, 240)), fg((255, 82, 162))
    sep = f" {dim}·{RESET} "
    if not st:
        parts = [f"{neon}AI-RPG-Player{RESET}", f"{ink}尚未开局，用 /newworld 或 /genesis 开启世界{RESET}"]
    else:
        w_items = st.get("world_status_items") or []
        if w_items:
            parts = [f"{neon if i == 0 else (cyan if w['key'] in ('loc', 'location') else ink)}{w['label']}: {w['value']}{RESET}" for i, w in enumerate(w_items)]
        else:
            parts = [f"{neon}第 {st['display_day']} 天{RESET}", f"{ink}{st['date']}{RESET}"]
            if st["loc"]:
                parts.append(f"{cyan}{st['loc']}{RESET}")
        if st["rank"]:
            parts.append(f"{ink}{st['rank']}{RESET}")
        if st["frozen"]:
            parts.append(f"{fg((120, 170, 230))}世界冻结中{RESET}")
    line = sep.join(parts)
    if model:
        line += f"   {dim}{model}{RESET}"
    return line


def get_status_payload(save_name=None, width=64, rows=4):
    st = game_state(save_name)
    sd = save_dir(save_name, required=False)
    pack_id = st["pack"] if st else active_pack_id(None)
    scene = st["scene"] if st else "megacity"
    seed = int(hashlib.md5(((st["loc_id"] if st else "start") or "start").encode()).hexdigest()[:8], 16)
    day = st["day"] if st else 0
    custom_sky = load_pack_skyline_config(pack_id, sd=sd)
    style = (custom_sky or {}).get("style")

    if style == "nature":
        px = paint_classic(width, rows, st["season"] if st else "春", scene, seed, day, custom_sky=custom_sky)
    elif style == "urban" or pack_id == "cyberpunk" or scene in CYBER_PALETTES:
        px = paint_cyber(width, rows, scene, seed, day, custom_sky=custom_sky)
    else:
        px = paint_classic(width, rows, st["season"] if st else "春", scene, seed, day, custom_sky=custom_sky)
    return {"state": st, "pixels": px}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", help="指定存档名")
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--width", type=int, default=64)
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式供 Web UI 渲染")
    args, _ = ap.parse_known_args()

    payload = get_status_payload(args.save, args.width, max(1, args.rows))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return

    lines = []
    if args.rows > 0:
        lines += render_ansi(payload["pixels"])
    lines.append(info_line(payload["state"]))
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
