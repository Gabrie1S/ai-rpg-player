"""世界工坊热插拔包管理、深度体检医生（Pack Doctor）、嵌套容错与三模式导出工具。

核心特性：
  1. 独立文件夹包热插拔（Drag & Drop）与按需扫描：
     - 玩家将世界观文件夹拖入 `packs/<folder>/`，调用 `list_packs()` 按需扫描（不做后台轮询）。
     - 自动识别三种状态：
       * `ready`：标准世界包（包含 `pack.yaml` 与 `rules.yaml`）
       * `nested`：解压或拖入时多套了一层子文件夹（如 `packs/foo/foo/pack.yaml`），支持一键 `flatten_pack()` 扁平化修复
       * `unparsed`：散装设定稿文件夹（`.md`/`.txt`），生成结构化缺失项与转换清单供 AI 原地编译，绝不导致引擎崩溃
     - 直接删除 `packs/<folder>/` 即可卸载，主程序自动平滑降级。
  2. 世界包深度体检器（Pack Doctor）：
     - 检验核心文件、YAML/JSON 语法、`rules.yaml` 阶位与属性区间、`pack.yaml` 的 `ui_schema` 控件类型与 `schema_mapping` 交叉自洽性、`theme.json` CSS 变量与像素天际线 RGB 色板。
  3. 三模式导出与导入：
     - 模式一：导出「纯净题材规则包」（Rule Pack .rpgpack）
     - 模式二：导出「已生成的世界母本种子」（Seeded World .rpgpack，自动抹除个人游玩痕迹且静默防剧透）
     - 模式三：导出「跑团小说成品」（Novel Book .md / .html）
     - 导入 `.rpgpack`、普通 `.zip` 或文件夹（自动处理嵌套层级）
"""
import argparse
import base64
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_event  # noqa: E402
from common import (  # noqa: E402
    GIT_IDENTITY,
    EXPORTS,
    PACKS,
    SAVES,
    active_pack_id,
    child_directory,
    load_ui_schema,
    load_world,
    pack_dir,
    read_frontmatter,
    SKILLS,
    read_text,
    safe_load_yaml,
    save_dir,
    schema_map,
    write_frontmatter,
)
from newsave import build_initial_journal  # noqa: E402
from pack_setup import check_setup, load_setup, public_setup, ui_mode  # noqa: E402

EXECUTABLE_SUFFIXES = {".py", ".js", ".mjs", ".sh", ".bat", ".cmd", ".ps1", ".exe", ".dll", ".so"}
REQUIRED_PACK_FILES = ["pack.yaml", "setting.md", "rules.md", "rules.yaml", "protagonist.md", "style_pack.md"]
VALID_WIDGETS = {"badge", "bar", "fraction", "meter", "tags", "kv"}
VALID_SHEET_SECTIONS = {
    "status_widgets",
    "attributes",
    "core_engine",
    "affinities",
    "aptitude",
    "abilities",
    "skills",
    "gear",
    "reputation",
    "inventory",
}
MAPPED_PLAYER_KEYS = {
    "rank": "rank_field",
    "core_engine": "engine_field",
    "affinity": "affinity_field",
    "aptitude": "aptitude_field",
    "currency": "currency_field",
}
VALID_DIALOGUE_STYLES = {
    "cyber-chamfer",
    "classic-rpg",
    "parchment-scroll",
    "gothic-ornate",
    "terminal-crt",
    "minimal-glass",
}
VALID_PIXEL_SCENES = {
    "cyber_alley",
    "misty_mountains",
    "gothic_cathedral",
    "rust_wasteland",
    "star_sector",
    "custom_canvas",
}
VALID_PIXEL_WEATHERS = {"neon_rain", "embers", "snow", "fireflies", "starlight", "fog", "none"}
VALID_SPRITE_ANCHORS = {
    "left-top",
    "left-mid",
    "left-bottom",
    "right-top",
    "right-mid",
    "right-bottom",
    "box-corner-tl",
    "box-corner-tr",
    "box-corner-bl",
    "box-corner-br",
}


def normalize_theme_data(theme_data: dict) -> dict:
    """对 AI 生成或修改的 theme.json 执行无损归一化（如右侧补齐像素精灵矩阵行宽、规整 mood_presets）。"""
    if not isinstance(theme_data, dict):
        return theme_data
    pframe = theme_data.get("pixel_frame")
    if isinstance(pframe, dict) and isinstance(pframe.get("sprites"), list):
        for sp in pframe["sprites"]:
            if not isinstance(sp, dict):
                continue
            rows = sp.get("rows")
            if isinstance(rows, list) and rows and all(isinstance(r, str) for r in rows):
                cleaned_rows = [r for r in rows if len(r) > 0]
                if cleaned_rows:
                    max_w = max(len(r) for r in cleaned_rows)
                    sp["rows"] = [r.ljust(max_w, ".") for r in cleaned_rows]
    moods = theme_data.get("mood_presets")
    if isinstance(moods, dict):
        for mk, mv in list(moods.items()):
            if not isinstance(mv, dict):
                moods.pop(mk, None)
                continue
            if "label" not in mv:
                mv["label"] = str(mk)
    return theme_data


def detect_nested_pack_dir(d: Path):
    """检测是否存在解压多套一层子目录的情况（如 packs/my_pack/my_pack/pack.yaml）。
    若检测到嵌套目录，返回相对 d 的子目录路径字符串（如 'my_pack'），否则返回 None。
    """
    if not d or not d.is_dir() or (d / "pack.yaml").exists() or (d / "rules.yaml").exists():
        return None

    top_files = [
        f for f in d.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.name.lower() not in ("desktop.ini", "thumbs.db", ".ds_store")
    ]
    subdirs = [sd for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith(".") and sd.name != "__MACOSX"]

    # 情形 1：1~2 层子目录内直接含有标准 pack.yaml
    for sub in subdirs:
        if (sub / "pack.yaml").exists():
            return sub.relative_to(d).as_posix()
        sub_subdirs = [s2 for s2 in sub.iterdir() if s2.is_dir() and not s2.name.startswith(".")]
        if len(sub_subdirs) == 1 and (sub_subdirs[0] / "pack.yaml").exists():
            return sub_subdirs[0].relative_to(d).as_posix()

    # 情形 2：顶层没有任何文档，且只有唯一一个子文件夹，里面放着散装设定文档
    if not top_files and len(subdirs) == 1:
        inner = subdirs[0]
        inner_docs = [
            f for f in inner.rglob("*")
            if f.is_file() and f.suffix.lower() in (".md", ".txt", ".yaml", ".yml", ".json")
        ]
        if inner_docs:
            return inner.relative_to(d).as_posix()

    return None


def flatten_pack(pid):
    """将多套了层级的嵌套世界包目录安全提升至 packs/<pid>/ 根目录（使用临时中转目录避免同名冲突）。"""
    d = pack_dir(pid, required=False)
    if not d or not d.is_dir():
        raise ValueError(f"世界包目录不存在：packs/{pid}")
    nested_rel = detect_nested_pack_dir(d)
    if not nested_rel:
        return {"ok": True, "pack_id": pid, "flattened": False, "message": "目录层级正常，无需扁平化处理"}

    nested_dir = d / nested_rel
    with tempfile.TemporaryDirectory(dir=str(PACKS)) as tmp:
        stage = Path(tmp) / "flat"
        shutil.copytree(nested_dir, stage)
        shutil.rmtree(d)
        shutil.copytree(stage, d)

    doc = doctor_pack(pid)
    return {
        "ok": True,
        "pack_id": pid,
        "flattened": True,
        "from_subpath": nested_rel,
        "status": doc["status"],
        "message": f"已将 packs/{pid}/{nested_rel}/ 扁平化归位至 packs/{pid}/（当前状态：{doc['status']}）",
    }


def _check_rgb_tuple(val):
    return isinstance(val, list) and len(val) == 3 and all(isinstance(x, int) and 0 <= x <= 255 for x in val)


def check_ui_schema(ui, smap, errors, warnings):
    """校验 ui_schema 的控件和映射，世界包和存档 mods 共用。"""
    for bar_name in ("world_status", "player_status"):
        items = ui.get(bar_name)
        if items is None:
            continue
        if not isinstance(items, list):
            errors.append(f"ui_schema.{bar_name} 必须是列表")
            continue
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict) or not item.get("key"):
                errors.append(f"ui_schema.{bar_name} 第 {idx} 项缺少 key")
                continue
            wtype = item.get("widget", "badge" if bar_name == "world_status" else "kv")
            if wtype not in VALID_WIDGETS:
                errors.append(f"ui_schema.{bar_name}「{item.get('key')}」使用了未知控件类型「{wtype}」（合法：{sorted(VALID_WIDGETS)}）")
            if wtype in ("bar", "fraction", "meter") and "max" in item:
                if not isinstance(item["max"], (int, float)) or item["max"] <= 0:
                    errors.append(f"ui_schema.{bar_name}「{item.get('key')}」的 max 必须为正数")
            if bar_name == "player_status" and item["key"] in MAPPED_PLAYER_KEYS and item.get("field"):
                expected_field = smap.get(MAPPED_PLAYER_KEYS[item["key"]])
                if expected_field and item["field"] != expected_field:
                    warnings.append(
                        f"ui_schema.player_status「{item['key']}」声明 field='{item['field']}'，与 rules.yaml schema_mapping 的 '{expected_field}' 不一致（运行时将以 schema_mapping 为准）"
                    )

    cs = ui.get("character_sheet")
    if isinstance(cs, dict) and isinstance(cs.get("sections"), list):
        for sec in cs["sections"]:
            if isinstance(sec, dict) and sec.get("id") and sec["id"] not in VALID_SHEET_SECTIONS:
                warnings.append(f"ui_schema.character_sheet 包含未识别模块 id='{sec['id']}'")

    jsecs = ui.get("journal_sections")
    if jsecs is not None and (not isinstance(jsecs, list) or not jsecs):
        warnings.append("ui_schema.journal_sections 建议配置至少 1 个日志分栏")


def check_theme_data(theme_data, errors, warnings):
    """校验主题内容，世界包和存档 mods 共用。"""
    css_vars = theme_data.get("css_vars") or {}
    if not isinstance(css_vars, dict):
        errors.append("theme.json 的 css_vars 必须是键值对象")
    else:
        bad_keys = [k for k in css_vars if not str(k).startswith("--")]
        if bad_keys:
            warnings.append(f"theme.json css_vars 存在非 '--' 开头的变量名：{bad_keys}")
    sky = theme_data.get("skyline")
    if isinstance(sky, dict):
        if sky.get("style") and sky["style"] not in ("urban", "nature"):
            warnings.append(f"theme.json skyline.style='{sky['style']}' 未识别，建议使用 'urban' 或 'nature'")
        for ck in ("sky_top", "sky_bot", "far", "near", "ground", "accent1", "accent2", "highlight"):
            if ck in sky and not _check_rgb_tuple(sky[ck]):
                errors.append(f"theme.json skyline.{ck} 必须是长度为 3 的 RGB 整数数组 [0-255, 0-255, 0-255]")

    typo = theme_data.get("typography")
    if typo is not None and not isinstance(typo, dict):
        errors.append("theme.json 的 typography 必须是对象结构")

    dbox = theme_data.get("dialogue_box")
    if dbox is not None:
        if not isinstance(dbox, dict):
            errors.append("theme.json 的 dialogue_box 必须是对象结构")
        elif dbox.get("style") and dbox["style"] not in VALID_DIALOGUE_STYLES:
            errors.append(
                f"theme.json dialogue_box.style='{dbox['style']}' 未在内置造型库中（合法：{sorted(VALID_DIALOGUE_STYLES)}）"
            )

    pframe = theme_data.get("pixel_frame")
    if pframe is not None:
        if not isinstance(pframe, dict):
            errors.append("theme.json 的 pixel_frame 必须是对象结构")
        else:
            if pframe.get("scene") and pframe["scene"] not in VALID_PIXEL_SCENES:
                warnings.append(f"theme.json pixel_frame.scene='{pframe['scene']}' 未识别，将回退至默认像素构图")
            if pframe.get("weather") and pframe["weather"] not in VALID_PIXEL_WEATHERS:
                warnings.append(f"theme.json pixel_frame.weather='{pframe['weather']}' 未识别")
            pal = pframe.get("palette")
            if pal is not None and not isinstance(pal, dict):
                errors.append("theme.json pixel_frame.palette 必须是单字符键调色板字典")
            sprites = pframe.get("sprites")
            if sprites is not None:
                if not isinstance(sprites, list):
                    errors.append("theme.json pixel_frame.sprites 必须是像素精灵列表")
                else:
                    for s_idx, sp in enumerate(sprites, 1):
                        if not isinstance(sp, dict):
                            errors.append(f"pixel_frame.sprites 第 {s_idx} 项必须是对象")
                            continue
                        rows = sp.get("rows")
                        if not isinstance(rows, list) or not rows or not all(isinstance(r, str) and len(r) > 0 for r in rows):
                            errors.append(f"pixel_frame.sprites 第 {s_idx} 项缺少非空字符串矩阵 rows")
                        elif len({len(r) for r in rows}) != 1:
                            errors.append(f"pixel_frame.sprites 第 {s_idx} 项 rows 每行字符长度必须严格一致")
                        anc = sp.get("anchor")
                        if anc and anc not in VALID_SPRITE_ANCHORS:
                            errors.append(f"pixel_frame.sprites 第 {s_idx} 项使用了未知锚点 anchor='{anc}'（合法：{sorted(VALID_SPRITE_ANCHORS)}）")
    moods = theme_data.get("mood_presets")
    if moods is not None:
        if not isinstance(moods, dict):
            errors.append("theme.json 的 mood_presets 必须是键值对象（如 default / combat / mystery / rest）")
        else:
            for mk, mv in moods.items():
                if not isinstance(mv, dict):
                    errors.append(f"theme.json mood_presets.{mk} 必须是对象")
                    continue
                if mv.get("weather") and mv["weather"] not in VALID_PIXEL_WEATHERS:
                    warnings.append(f"theme.json mood_presets.{mk}.weather='{mv['weather']}' 未识别")
                if mv.get("css_vars") is not None and not isinstance(mv.get("css_vars"), dict):
                    errors.append(f"theme.json mood_presets.{mk}.css_vars 必须是键值对象")


def check_panels(panels, file_exists, errors, warnings):
    """校验自定义面板声明。面板在浏览器沙箱里运行，这里只查声明和文件。"""
    if panels is None:
        return
    if not isinstance(panels, list):
        errors.append("ui_schema.panels 必须是列表")
        return
    seen = set()
    for idx, panel in enumerate(panels, 1):
        if not isinstance(panel, dict):
            errors.append(f"ui_schema.panels 第 {idx} 项必须是字典")
            continue
        panel_id = str(panel.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", panel_id):
            errors.append(f"ui_schema.panels 第 {idx} 项的 id 只能用字母、数字、下划线和连字符")
            continue
        if panel_id in seen:
            errors.append(f"ui_schema.panels 的 id「{panel_id}」重复")
        seen.add(panel_id)
        if panel.get("enabled", True) is False:
            continue
        if not panel.get("label"):
            errors.append(f"面板「{panel_id}」缺少 label")
        rel = str(panel.get("file") or f"panels/{panel_id}.html")
        if not re.fullmatch(r"panels/[A-Za-z0-9_-]{1,48}\.html", rel):
            errors.append(f"面板「{panel_id}」的 file 必须是 panels/<名字>.html")
        elif not file_exists(rel):
            errors.append(f"面板「{panel_id}」的文件不存在：{rel}")


def _is_hidden_pack_file(rel):
    """这些文件不向玩家展示。文件名本身也可能剧透，所以连列表都不进。"""
    return rel == "backstage.md" or rel.startswith(("templates/", "commands/", "panels/"))


def doctor_pack(pid):
    """世界包深度体检医生（Pack Doctor）：检验文件完整性、YAML/JSON语法、Schema交叉映射与主题规范。"""
    d = pack_dir(pid, required=False)
    if not d or not d.is_dir():
        return {
            "id": str(pid),
            "exists": False,
            "status": "missing",
            "valid": False,
            "errors": [f"题材包目录不存在：packs/{pid}"],
            "warnings": [],
            "missing_files": list(REQUIRED_PACK_FILES),
            "nested_subpath": None,
            "conversion_plan": None,
        }

    errors, warnings = [], []
    nested_rel = detect_nested_pack_dir(d)
    inspect_root = (d / nested_rel) if nested_rel else d

    has_pack_yaml = (inspect_root / "pack.yaml").exists()
    has_rules_yaml = (inspect_root / "rules.yaml").exists()
    missing_files = [f for f in REQUIRED_PACK_FILES if not (inspect_root / f).exists()]
    raw_files = [
        f.relative_to(d).as_posix()
        for f in sorted(d.rglob("*"))
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in (".md", ".txt", ".yaml", ".yml", ".json")
        and not _is_hidden_pack_file(f.relative_to(inspect_root if f.is_relative_to(inspect_root) else d).as_posix())
    ]

    if nested_rel:
        status = "nested"
        errors.append(
            f"检测到嵌套子目录 `packs/{pid}/{nested_rel}/`，请点击「扁平化修复目录」或运行 `python engine/tools/pack_io.py flatten {pid}`"
        )
    elif has_pack_yaml and has_rules_yaml:
        status = "ready"
    else:
        status = "unparsed"

    if missing_files:
        errors.append(f"缺少核心标准文件：{missing_files}")

    # 1. 校验 pack.yaml 与 rules.yaml 语法及字段
    pack_meta, pack_err = safe_load_yaml(inspect_root / "pack.yaml")
    if pack_err:
        errors.append(pack_err)
    rules_meta, rules_err = safe_load_yaml(inspect_root / "rules.yaml")
    if rules_err:
        errors.append(rules_err)

    if has_rules_yaml and not rules_err:
        ranks = (rules_meta or {}).get("ranks")
        if not isinstance(ranks, list) or not ranks:
            errors.append("rules.yaml 缺少非空的 ranks 阶位定义列表")
        else:
            for idx, r in enumerate(ranks, 1):
                if not isinstance(r, dict) or not r.get("name"):
                    errors.append(f"rules.yaml ranks 第 {idx} 项缺少 name 字段")
                elif "attr_range" in r:
                    ar = r["attr_range"]
                    if not (isinstance(ar, list) and len(ar) == 2 and all(isinstance(x, (int, float)) for x in ar) and ar[0] <= ar[1]):
                        errors.append(f"rules.yaml 阶位「{r['name']}」的 attr_range 应为 [最小值, 最大值]")
        attrs = (rules_meta or {}).get("attributes")
        if not isinstance(attrs, list) or not attrs:
            errors.append("rules.yaml 缺少非空的 attributes 核心属性列表")

    # 世界包是可分发的内容，不能带玩家的驱动配置或可执行代码
    if has_pack_yaml and not pack_err:
        errors.extend(check_setup((pack_meta or {}).get("setup")))
        leaked = sorted(set(pack_meta or {}) & {"front_driver", "back_driver", "api_key", "model"})
        if leaked:
            warnings.append(f"pack.yaml 含有玩家配置字段 {leaked}：模型和密钥只放在 config.yaml，引擎不会读取这些字段")
        calendar = (pack_meta or {}).get("calendar")
        start_year = calendar.get("start_year") if isinstance(calendar, dict) else None
        if not isinstance(calendar, dict):
            warnings.append("pack.yaml 没有配置 calendar：开局日期会用引擎模板里的默认历法，很可能和这个世界对不上")
        elif isinstance(start_year, int) and start_year <= 1:
            warnings.append("calendar.start_year 是 1：世界看起来像是因主角才开始的。除非确实是纪元元年，否则写世界已经延续到的年份")
        optional_skills = (pack_meta or {}).get("optional_skills") or []
        if not isinstance(optional_skills, list):
            errors.append("pack.yaml optional_skills 应为列表")
            optional_skills = []
        for sk in optional_skills:
            if not isinstance(sk, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", sk) or not (SKILLS / sk / "SKILL.md").is_file():
                warnings.append(f"optional_skills 引用了不存在的技能「{sk}」")
    code_files = sorted(
        f.relative_to(inspect_root).as_posix()
        for f in inspect_root.rglob("*")
        if f.is_file() and ".slices" not in f.parts and f.suffix.lower() in EXECUTABLE_SUFFIXES
    )
    if code_files:
        warnings.append(f"世界包包含可执行文件 {code_files[:5]}：引擎不会运行它们。机制请用命令与规则文档实现，界面请用 panels/*.html（在浏览器沙箱里运行）")
    commands_dir = inspect_root / "commands"
    if commands_dir.is_dir():
        for cmd_file in commands_dir.glob("*.md"):
            try:
                _, cmd_body = read_frontmatter(cmd_file)
            except (ValueError, yaml.YAMLError) as exc:
                errors.append(f"commands/{cmd_file.name} 格式错误：{exc}")
                continue
            if not (cmd_body or "").strip():
                errors.append(f"commands/{cmd_file.name} 没有正文")
            elif "$ARGUMENTS" not in cmd_body:
                warnings.append(f"commands/{cmd_file.name} 没有使用 $ARGUMENTS，玩家的输入不会传给命令")

    # 2. 深度检验 ui_schema 与 rules.yaml schema_mapping 的交叉自洽性
    smap = schema_map(rules_meta or {})
    if has_pack_yaml and not pack_err:
        ui = (pack_meta or {}).get("ui_schema")
        if ui is not None and not isinstance(ui, dict):
            errors.append("pack.yaml 的 ui_schema 必须是字典结构")
        elif isinstance(ui, dict):
            check_ui_schema(ui, smap, errors, warnings)
            check_panels(ui.get("panels"), lambda rel: (inspect_root / rel).is_file(), errors, warnings)
        else:
            warnings.append("pack.yaml 未配置 ui_schema，将自动回退使用内置通用状态栏与日志分栏")

    # 3. 检验 theme.json 与像素天际线色板
    theme_data = {}
    theme_path = inspect_root / "theme.json"
    if theme_path.exists():
        try:
            theme_data = json.loads(read_text(theme_path))
            if not isinstance(theme_data, dict):
                errors.append("theme.json 顶层必须是 JSON 对象")
                theme_data = {}
            else:
                check_theme_data(theme_data, errors, warnings)
        except Exception as e:
            errors.append(f"theme.json JSON 语法错误：{e}")
    elif status == "ready":
        warnings.append("尚未提供专属 theme.json（可在创世向导阶段三或右侧场外调教窗一键生成）")

    conversion_plan = None
    if status in ("unparsed", "nested"):
        conversion_plan = {
            "source_files": raw_files,
            "missing_standard_files": missing_files + ([] if theme_path.exists() else ["theme.json"]),
            "recommended_action": "flatten_then_parse" if status == "nested" else "ai_parse_to_pack",
        }

    is_valid = len(errors) == 0 and status == "ready"
    if errors:
        log_event(
            "pack.doctor_issue",
            lvl="WARN",
            pack_id=d.name,
            status=status,
            errors=errors,
            warnings=warnings,
        )
    return {
        "id": d.name,
        "exists": True,
        "status": status,
        "valid": is_valid,
        "errors": errors,
        "warnings": warnings,
        "missing_files": missing_files,
        "nested_subpath": nested_rel,
        "raw_files": raw_files,
        "pack_meta": pack_meta or {},
        "rules_meta": rules_meta or {},
        "theme": theme_data,
        "conversion_plan": conversion_plan,
    }


def validate_pack(pid):
    rep = doctor_pack(pid)
    return rep["valid"], rep["errors"]


def list_packs():
    """按需扫描 packs/ 下的所有独立文件夹包（兼容标准包、嵌套文件夹与待解析散装设定文件夹）。"""
    PACKS.mkdir(parents=True, exist_ok=True)
    cur_pack = active_pack_id()
    out = []
    for d in sorted(PACKS.iterdir()):
        if d.name.startswith(".") or not pack_dir(d.name, required=False):
            continue
        rep = doctor_pack(d.name)
        meta = rep["pack_meta"]
        status = rep["status"]
        raw_files = rep["raw_files"]

        if status == "nested":
            default_desc = f"检测到嵌套子文件夹 `{rep['nested_subpath']}/`（共 {len(raw_files)} 个文件），点击「扁平化修复」即可归位。"
            default_genre = "嵌套目录待修复"
        elif status == "unparsed":
            default_desc = f"检测到散装设定文件夹（含 {len(raw_files)} 个文件），可通过创世向导让 AI 自动解析为标准世界包。"
            default_genre = "待解析设定包"
        else:
            default_desc = ""
            default_genre = ""

        out.append({
            "id": d.name,
            "name": meta.get("name") or d.name,
            "genre": meta.get("genre") or default_genre,
            "author": meta.get("author", ""),
            "description": meta.get("description") or default_desc,
            "version": str(meta.get("version", "1.0")),
            "status": status,
            "nested_subpath": rep["nested_subpath"],
            "valid": rep["valid"],
            "errors": rep["errors"],
            "warnings": rep["warnings"],
            "missing_files": rep["missing_files"],
            "conversion_plan": rep["conversion_plan"],
            "is_active": d.name == cur_pack,
            "raw_files": raw_files,
            "theme": rep["theme"],
            "ui_schema": load_ui_schema(pack_id=d.name) if status == "ready" else {},
            "setup": public_setup(load_setup(d)) if status == "ready" else [],
            "ui_mode": ui_mode(load_setup(d), rep["theme"]) if status == "ready" else "ask",
        })
    return out


def inspect_pack_detail(pid):
    """读取指定世界包文件夹的完整详情、Schema 摘要、深度体检报告与设定文档内容。"""
    d = pack_dir(pid, required=False)
    if not d or not d.is_dir():
        raise ValueError(f"世界包文件夹不存在：packs/{pid}")

    packs = list_packs()
    summary = next((p for p in packs if p["id"] == pid), None) or {"id": pid, "name": pid}
    nested_rel = summary.get("nested_subpath")
    inspect_root = (d / nested_rel) if nested_rel else d
    rules, _ = safe_load_yaml(inspect_root / "rules.yaml")
    rules = rules or {}

    rules_summary = {
        "schema_mapping": rules.get("schema_mapping") or {},
        "ranks": [r.get("name") if isinstance(r, dict) else str(r) for r in (rules.get("ranks") or [])],
        "levels": [str(x) for x in (rules.get("levels") or [])],
        "attributes": [str(x) for x in (rules.get("attributes") or [])],
        "affinity_grades": [str(x) for x in (rules.get("affinity_grades") or [])],
        "gear_tiers": [str(x) for x in (rules.get("gear_tiers") or [])],
    }

    docs = {}
    standard_docs = ["setting.md", "rules.md", "protagonist.md", "style_pack.md", "pack.yaml", "rules.yaml", "theme.json"]
    for fname in standard_docs:
        fp = inspect_root / fname
        if fp.exists() and fp.is_file():
            docs[fname] = read_text(fp)[:12000]

    if not docs:
        for rel in summary.get("raw_files") or []:
            fp = d / rel
            if fp.exists() and fp.is_file():
                docs[rel] = read_text(fp)[:12000]

    return {
        **summary,
        "rules_summary": rules_summary,
        "docs": docs,
    }


def export_pack(pid, out_path=None):
    """模式一：导出纯净题材规则包。"""
    d = pack_dir(pid, required=True)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(out_path) if out_path else (EXPORTS / f"{pid}-pack.rpgpack")
    out.parent.mkdir(parents=True, exist_ok=True)

    manifest = {"bundle_type": "rule_pack", "pack_id": pid}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rpgpack_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for f in d.rglob("*"):
            rel = f.relative_to(d).as_posix()
            # .slices 是运行时切片缓存，随时可重建，不进导出包
            if f.is_file() and not rel.startswith(".slices/"):
                zf.write(f, f"pack/{rel}")
    return out


def export_seed(sd, out_path=None):
    """模式二：导出已生成的世界母本种子（抹除个人游玩痕迹，保留全部生成的世界设定/NPC/势力/阴谋，静默防剧透）。"""
    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(out_path) if out_path else (EXPORTS / f"{sd.name}-seed.rpgpack")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        stage_dir = Path(tmp) / "seed"
        shutil.copytree(sd, stage_dir, ignore=shutil.ignore_patterns(".git", "__pycache__", "seed_status.yaml", "candidate.yaml"))

        for sub in ("story", "sessions", "snapshots"):
            p = stage_dir / "log" / sub
            if p.is_dir():
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)
        for lf in ("turns.tsv", "dice.log", "tuner_history.json"):
            p = stage_dir / "log" / lf
            if p.exists():
                p.unlink()
        (stage_dir / "log" / "summaries.md").write_text("# 章节摘要\n\n", encoding="utf-8")
        (stage_dir / "log" / "milestones.md").write_text(
            "# 重要时刻\n\n主角一生中的关键时刻，按时间排列。**永不压缩、永不删改。** 主角视角，不含秘密。\n",
            encoding="utf-8",
        )
        (stage_dir / "log" / "changelog.md").write_text(
            "# 上帝视角（/gm）改动记录\n\n| 时间 | 游戏日 | 来源 | 改动 | 连锁影响 |\n| --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

        ui_schema = load_ui_schema(stage_dir)
        (stage_dir / "player" / "journal.md").write_text(
            build_initial_journal(ui_schema),
            encoding="utf-8",
        )
        (stage_dir / "player" / "persona.md").write_text(
            "# 主角的性格与习惯\n\n从玩家的实际选择里总结出来的。主持人遇到同类的小决定，按这里直接写主角怎么做，不再停下来问。\n\n## 性格\n\n## 待人\n\n## 处事习惯\n\n## 说话方式\n\n## 依据\n",
            encoding="utf-8",
        )

        now_path = stage_dir / "world" / "now.md"
        if now_path.exists():
            now_meta, _ = read_frontmatter(now_path)
            now_meta = now_meta or {}
            now_meta["day"] = 0
            now_meta["last_tick_day"] = 0
            now_meta["last_snapshot_day"] = 0
            now_meta["frozen"] = False
            now_meta["volume"] = 1
            now_meta["recent_challenges"] = []
            write_frontmatter(now_path, now_meta, "\n## 当前场景\n\n第 0 天，开局场景。\n\n## 进行中的事\n\n## 待处理\n")

        npcs_dir = stage_dir / "world" / "npcs"
        if npcs_dir.is_dir():
            for np in npcs_dir.glob("*.md"):
                try:
                    m, body = read_frontmatter(np)
                    if m:
                        m["met_player"] = None
                        m["last_seen"] = None
                        if "## 与玩家的记忆" in body:
                            pre, post = body.split("## 与玩家的记忆", 1)
                            rest = "\n## " + post.split("\n## ", 1)[1] if "\n## " in post else ""
                            body = pre + "## 与玩家的记忆\n\n<!-- - 第N天：摘要 -->\n" + rest
                        write_frontmatter(np, m, body)
                except Exception:
                    pass

        try:
            from index import build
            build(stage_dir)
        except Exception:
            pass

        w = load_world(stage_dir)
        manifest = {
            "bundle_type": "seeded_world",
            "world_name": w.get("name", sd.name),
            "pack_id": w.get("pack", ""),
            "genre": w.get("genre", ""),
        }
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("rpgpack_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for f in stage_dir.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(stage_dir).as_posix()
                    zf.write(f, f"seed/{rel}")
    return out


def export_novel(sd, fmt="md", out_path=None):
    """模式三：将存档中的 log/story/ 原文档案与 milestones.md 编译为排版优美的单本跑团小说。"""
    EXPORTS.mkdir(parents=True, exist_ok=True)
    w = load_world(sd)
    title = w.get("name", sd.name)
    story_dir = sd / "log" / "story"
    chapters = []
    if story_dir.is_dir():
        for vol_dir in sorted(story_dir.iterdir()):
            if not vol_dir.is_dir():
                continue
            vol_files = sorted(vol_dir.glob("*.md"))
            if not vol_files:
                continue
            chapters.append((vol_dir.name, [(f.stem, read_text(f).strip()) for f in vol_files]))

    ms_path = sd / "log" / "milestones.md"
    milestones = read_text(ms_path).strip() if ms_path.exists() else ""

    if fmt == "html":
        out = Path(out_path) if out_path else child_directory(EXPORTS, f"{sd.name}-跑团小说.html")
        body_parts = [f"<h1>《{html.escape(title)}》跑团实录</h1>"]
        for vol_name, files in chapters:
            body_parts.append(f"<h2 class='vol'>{html.escape(vol_name)}</h2>")
            for stem, content in files:
                paras = []
                for p in content.split("\n\n"):
                    p = p.strip()
                    if not p or p == "---":
                        continue
                    if p.startswith("# "):
                        paras.append(f"<h3>{html.escape(p[2:])}</h3>")
                    elif p.startswith("> ") or p.startswith("**你：**"):
                        clean_q = p.replace("**你：**", "").replace("> ", "").strip()
                        paras.append(f"<div class='player-act'><strong>【你的行动】</strong> {html.escape(clean_q)}</div>")
                    else:
                        paras.append(f"<p>{html.escape(p)}</p>")
                body_parts.append("<section class='day-entry'>" + "\n".join(paras) + "</section>")
        if milestones:
            body_parts.append(f"<hr><section class='milestones'><pre>{html.escape(milestones)}</pre></section>")
        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>《{html.escape(title)}》跑团小说</title>
<style>
  body {{ max-width: 780px; margin: 40px auto; padding: 0 24px; font-family: "Noto Serif SC", "Songti SC", serif; line-height: 1.85; background: #faf8f5; color: #222; }}
  h1 {{ text-align: center; border-bottom: 2px solid #333; padding-bottom: 16px; }}
  h2.vol {{ margin-top: 48px; color: #555; border-left: 4px solid #333; padding-left: 12px; }}
  h3 {{ margin-top: 32px; color: #444; }}
  .player-act {{ background: #eeeadd; padding: 10px 14px; border-radius: 6px; margin: 16px 0; font-size: 0.95em; color: #444; }}
  p {{ text-indent: 2em; margin: 12px 0; }}
</style>
</head>
<body>
{''.join(body_parts)}
</body>
</html>"""
        out.write_text(html_doc, encoding="utf-8")
        return out

    out = Path(out_path) if out_path else child_directory(EXPORTS, f"{sd.name}-跑团小说.md")
    md_parts = [f"# 《{title}》跑团小说实录\n"]
    for vol_name, files in chapters:
        md_parts.append(f"\n## {vol_name}\n")
        for _, content in files:
            md_parts.append(content + "\n")
    if milestones:
        md_parts.append("\n---\n\n" + milestones + "\n")
    out.write_text("\n".join(md_parts), encoding="utf-8")
    return out


MAX_ARCHIVE_FILES = 2000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024


def _import_file(dest, filename):
    parts = filename.replace("\\", "/").split("/")
    path = dest
    for part in parts:
        if part.casefold() in (".git", "git~1"):
            raise ValueError("导入文件不能包含 Git 仓库元数据")
        path = child_directory(path, part)
    return path


def _safe_extract_zip(zf, dest, prefix=""):
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_FILES or sum(i.file_size for i in infos) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("压缩包超出 2000 个成员或 128MB 的解压上限")
    for info in infos:
        name = info.filename.replace("\\", "/")
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or rel.startswith("__MACOSX/"):
            continue
        if prefix == "seed/" and rel.casefold() in ("world/candidate.yaml", "world/seed_status.yaml"):
            # 兼容旧导出包，同时不从归档导入本机任务状态。
            continue
        target = _import_file(dest, rel.rstrip("/") if info.is_dir() else rel)
        if info.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(zf.read(info))


def _install_import(root, name, populate, *, flatten=False):
    dest = child_directory(root, name)
    root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise ValueError(f"「{name}」已存在，请换一个导入名称")
    with tempfile.TemporaryDirectory(dir=root, prefix=".import-") as tmp:
        stage = Path(tmp) / "content"
        stage.mkdir()
        populate(stage)
        if not any(stage.iterdir()):
            raise ValueError("导入内容为空")
        nested = detect_nested_pack_dir(stage) if flatten else None
        if dest.exists():
            raise ValueError(f"「{name}」已存在，请换一个导入名称")
        (stage / nested if nested else stage).rename(dest)
    return dest


def _bundle_manifest(zf):
    name = "rpgpack_manifest.json"
    if name not in zf.namelist():
        return {}
    if zf.getinfo(name).file_size > 64 * 1024:
        raise ValueError("rpgpack 清单超过 64KB")
    manifest = json.loads(zf.read(name))
    if not isinstance(manifest, dict) or manifest.get("bundle_type") not in ("rule_pack", "seeded_world"):
        raise ValueError("未知的 rpgpack 类型")
    return manifest


def import_bundle(src_path, target_name=None):
    """导入 .rpgpack、普通 .zip 或文件夹，不覆盖已有包或存档。"""
    src = Path(src_path)
    if not src.exists():
        raise ValueError(f"找不到要导入的路径：{src}")

    if src.is_dir():
        if PACKS.resolve().is_relative_to(src.resolve()):
            raise ValueError("导入源不能包含世界包安装目录")
        pid = target_name or src.name
        dest = _install_import(PACKS, pid, lambda stage: shutil.copytree(
            src, stage, dirs_exist_ok=True, ignore=lambda _, names: [n for n in names if n.casefold() == ".git"]
        ), flatten=True)
        return {"type": "folder_pack", "id": pid, "path": str(dest)}

    with zipfile.ZipFile(src) as zf:
        manifest = _bundle_manifest(zf)
        btype = manifest.get("bundle_type", "zip_pack")
        is_seed = btype == "seeded_world"
        name = target_name or manifest.get("world_name" if is_seed else "pack_id")
        default_name = src.stem.removesuffix("-seed" if is_seed else "-pack") if manifest else src.stem
        name = name or default_name
        prefix = {"rule_pack": "pack/", "seeded_world": "seed/"}.get(btype, "")
        dest = _install_import(
            SAVES if is_seed else PACKS, name, lambda stage: _safe_extract_zip(zf, stage, prefix),
            flatten=btype == "zip_pack",
        )
    if is_seed and shutil.which("git"):
        try:
            subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
            subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "第0天：导入世界母本"], cwd=dest, check=True)
        except (OSError, subprocess.CalledProcessError):
            pass
    return {"type": btype, "id": name, "path": str(dest)}


def _sanitize_id(raw_name, fallback="imported_pack"):
    s = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", str(raw_name or "").strip()).strip("_")
    return s or fallback


def import_uploaded_payload(payload: dict):
    """导入拖入的归档或设定文件，发布到新目录后执行 Pack Doctor。"""
    archive_b64 = (payload.get("archive_b64") or payload.get("data_base64") or "").strip()
    filename = (payload.get("filename") or "uploaded.zip").strip()
    target_name = (payload.get("target_name") or "").strip()
    stem = Path(filename).stem

    if archive_b64:
        raw_bytes = base64.b64decode(archive_b64, validate=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_zip = Path(tmp) / "upload.zip"
            tmp_zip.write_bytes(raw_bytes)
            with zipfile.ZipFile(tmp_zip) as zf:
                manifest = _bundle_manifest(zf)
            if manifest.get("bundle_type") == "seeded_world":
                name = _sanitize_id(target_name or manifest.get("world_name") or stem.removesuffix("-seed"), "导入世界")
                base, idx = name, 2
                while child_directory(SAVES, name).exists():
                    name = f"{base}_{idx}"
                    idx += 1
                res = import_bundle(tmp_zip, target_name=name)
                log_event("pack.upload_seed", save_name=name, pack_id=manifest.get("pack_id", ""))
                return {"ok": True, "import_kind": "seeded_world", "id": name,
                        "pack_id": manifest.get("pack_id", ""), "bundle": res}

            pid = _sanitize_id(target_name or manifest.get("pack_id") or stem.removesuffix("-pack"))
            details = {"bundle": import_bundle(tmp_zip, target_name=pid)}
    else:
        files = payload.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("未收到有效的压缩包或设定文件列表")
        pid = _sanitize_id(target_name or payload.get("pack_id") or stem)

        def populate(stage):
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("设定文件条目格式错误")
                rel = str(item.get("path") or item.get("name") or "").replace("\\", "/")
                path = _import_file(stage, rel)
                top, sep, rest = rel.partition("/")
                if sep and _sanitize_id(top) == pid:
                    path = _import_file(stage, rest)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as out:
                    out.write(str(item.get("content") or ""))

        _install_import(PACKS, pid, populate, flatten=True)
        details = {"files_written": len(files)}

    doc = doctor_pack(pid)
    log_event("pack.upload", pack_id=pid, status=doc["status"], valid=doc["valid"])
    return {"ok": True, "import_kind": "pack", "id": pid, "status": doc["status"],
            "valid": doc["valid"], "doctor": doc, **details}


def convert_sillytavern_lorebook(json_path, out_dir):
    """将 SillyTavern 的 World Info (Lorebook .json) 转换为 Markdown 词条草案，供 AI 编译入库。"""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    entries = data.get("entries") or {}
    if isinstance(entries, dict):
        entries = list(entries.values())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            continue
        keys = e.get("key") or e.get("keys") or []
        comment = e.get("comment") or (keys[0] if keys else f"entry_{i}")
        content = (e.get("content") or "").strip()
        if not content:
            continue
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(comment)).strip("_")[:32] or f"entry_{i}"
        meta = {"source": "sillytavern_lorebook", "keys": keys, "title": str(comment)}
        write_frontmatter(out / f"{i:03d}-{safe}.md", meta, f"\n# {comment}\n\n{content}\n")
        count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description="世界工坊热插拔包管理、深度体检与三模式导出工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp1 = sub.add_parser("list-packs")
    lp1.add_argument("--json", action="store_true")
    lp2 = sub.add_parser("list")
    lp2.add_argument("--json", action="store_true")

    vp = sub.add_parser("validate-pack")
    vp.add_argument("pack_id")

    doc_p = sub.add_parser("doctor")
    doc_p.add_argument("pack_id")
    doc_p.add_argument("--json", action="store_true")

    ins = sub.add_parser("inspect")
    ins.add_argument("pack_id")
    ins.add_argument("--json", action="store_true")

    fl = sub.add_parser("flatten")
    fl.add_argument("pack_id")

    ep = sub.add_parser("export-pack")
    ep.add_argument("pack_id")
    ep.add_argument("--out", help="输出路径")

    es = sub.add_parser("export-seed")
    es.add_argument("--save", help="存档名，默认当前存档")
    es.add_argument("--out", help="输出路径")

    en = sub.add_parser("export-novel")
    en.add_argument("--save", help="存档名，默认当前存档")
    en.add_argument("--format", choices=["md", "html"], default="md")
    en.add_argument("--out", help="输出路径")

    im = sub.add_parser("import")
    im.add_argument("src", help=".rpgpack 文件或设定文件夹路径")
    im.add_argument("--name", help="指定导入后的题材包名或存档名")

    cl = sub.add_parser("convert-lorebook")
    cl.add_argument("json_path", help="SillyTavern World Info JSON 路径")
    cl.add_argument("--out", required=True, help="输出草案目录")

    args = ap.parse_args()

    try:
        if args.cmd in ("list-packs", "list"):
            packs = list_packs()
            if args.json:
                print(json.dumps(packs, ensure_ascii=False, indent=2))
                return 0
            if not packs:
                print("（packs/ 目录当前为空，可使用创世向导 /genesis 或 /newpack 创建世界包）")
                return 0
            status_map = {"ready": "✓ 已就绪", "nested": "🔧 嵌套待扁平化", "unparsed": "⚠ 待AI解析"}
            for p in packs:
                tag = status_map.get(p["status"], p["status"])
                print(f"- {p['id']} [{tag}]：{p['name']} ({p['genre']})  {p['description']}")

        elif args.cmd in ("validate-pack", "doctor", "inspect"):
            if args.cmd == "inspect" and getattr(args, "json", False):
                print(json.dumps(inspect_pack_detail(args.pack_id), ensure_ascii=False, indent=2))
                return 0
            rep = doctor_pack(args.pack_id)
            if getattr(args, "json", False):
                print(json.dumps(rep, ensure_ascii=False, indent=2))
                return 0 if rep["valid"] else 1
            print(f"世界包「{args.pack_id}」体检结果：状态={rep['status']}，合规={'是' if rep['valid'] else '否'}")
            for e in rep["errors"]:
                print(f"  ✗ {e}")
            for w in rep["warnings"]:
                print(f"  ! {w}")
            if rep["valid"] and not rep["warnings"]:
                print("  ✓ 包结构、Schema 映射与主题配置全部通过检验")
            return 0 if rep["valid"] else 1

        elif args.cmd == "flatten":
            res = flatten_pack(args.pack_id)
            print(f"✓ {res['message']}")

        elif args.cmd == "export-pack":
            out = export_pack(args.pack_id, args.out)
            print(f"✓ 已导出纯净题材规则包：{out}")
        elif args.cmd == "export-seed":
            sd = save_dir(args.save)
            out = export_seed(sd, args.out)
            print(f"✓ 已静默导出脱敏世界母本种子（第0天开局态，不含个人日志与剧透回显）：{out}")
        elif args.cmd == "export-novel":
            sd = save_dir(args.save)
            out = export_novel(sd, args.format, args.out)
            print(f"✓ 已导出跑团小说成品：{out}")
        elif args.cmd == "import":
            res = import_bundle(args.src, args.name)
            print(f"✓ 已导入 {res['type']}「{res['id']}」至 {res['path']}")
        elif args.cmd == "convert-lorebook":
            n = convert_sillytavern_lorebook(args.json_path, args.out)
            print(f"✓ 已从 SillyTavern 世界书提取 {n} 条词条草案至 {args.out}")
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
