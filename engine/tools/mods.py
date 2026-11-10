"""存档定制层 saves/<存档>/mods/：玩家和玩家的 AI 自由改造这一局，不碰世界包。

覆盖顺序：引擎默认 < 世界包 < 存档 mods。结构与世界包相同，只写想改的部分：
  pack.yaml        只读 ui_schema，与世界包合并
  theme.json       与世界包主题深度合并
  setting.md rules.md protagonist.md style_pack.md backstage.md   追加在世界包文档之后
  authors_note.md  替换这一局每轮置底的叙事提示词
  commands/<名字>.md   自定义命令，优先于世界包和通用命令
  panels/<id>.html     浏览器沙箱里运行的自定义面板
  data/<id>.json       面板数据，主持人可以随剧情更新

mods 在存档的 Git 仓库里，撤回回合时保留。每次写入都先校验，不通过就原样恢复。

  python engine/tools/mods.py list [--save 存档]
  python engine/tools/mods.py check [--save 存档]
  python engine/tools/mods.py remove <相对路径> [--save 存档]
  python engine/tools/mods.py requests        查看 AI 记录的扩展需求
"""
import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_event  # noqa: E402
from apply import git_turn_checkpoint  # noqa: E402
from common import (  # noqa: E402
    ROOT,
    active_pack_id,
    deep_merge,
    load_theme,
    load_ui_schema,
    mods_dir,
    pack_dir,
    read_text,
    safe_load_yaml,
    save_dir,
    schema_map,
)
from pack_io import check_panels, check_theme_data, check_ui_schema, normalize_theme_data  # noqa: E402

DOC_FILES = ("setting.md", "rules.md", "protagonist.md", "style_pack.md", "backstage.md", "authors_note.md")
ID_RE = r"[A-Za-z0-9_-]{1,48}"
MAX_FILE_BYTES = 256 * 1024
REQUESTS_LOG = ROOT / "logs" / "extension_requests.jsonl"


def clean_rel(rel):
    """校验 mods 内的相对路径，只允许已知的几类文件。"""
    path = str(rel or "").replace("\\", "/").strip().lstrip("/")
    allowed = (
        path in ("pack.yaml", "theme.json")
        or path in DOC_FILES
        or re.fullmatch(rf"commands/{ID_RE}\.md", path)
        or re.fullmatch(rf"panels/{ID_RE}\.html", path)
        or re.fullmatch(rf"data/{ID_RE}\.json", path)
    )
    if not allowed:
        raise ValueError(f"不能写入 mods/{path}：只允许 pack.yaml、theme.json、{list(DOC_FILES)}、commands/*.md、panels/*.html、data/*.json")
    return path


def _files(sd):
    root = mods_dir(sd)
    if not root or not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def snapshot(sd):
    root = mods_dir(sd)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in _files(sd)}


def restore_snapshot(sd, snap):
    root = mods_dir(sd)
    if root.is_dir():
        shutil.rmtree(root)
    for rel, raw in (snap or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def _mod_yaml(sd):
    path = mods_dir(sd) / "pack.yaml"
    if not path.exists():
        return {}, None
    meta, err = safe_load_yaml(path)
    return meta if isinstance(meta, dict) else {}, err


def list_mods(sd):
    """给界面和 AI 看的定制清单。"""
    if not sd:
        return []
    root = mods_dir(sd)
    labels = {str(p.get("id")): p.get("label") for p in (_mod_yaml(sd)[0].get("ui_schema") or {}).get("panels") or [] if isinstance(p, dict)}
    out = []
    for p in _files(sd):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("panels/"):
            kind, label = "面板", labels.get(p.stem) or p.stem
        elif rel.startswith("commands/"):
            kind, label = "命令", f"/{p.stem}"
        elif rel.startswith("data/"):
            continue  # 面板数据跟着面板走，不单独列
        elif rel == "theme.json":
            kind, label = "界面", "配色与主题"
        elif rel == "pack.yaml":
            kind, label = "界面", "状态栏与面板设置"
        else:
            kind, label = "文档", rel
        out.append({"path": rel, "kind": kind, "label": str(label), "bytes": p.stat().st_size})
    return out


def check_mods(sd):
    """按合并后的最终效果校验，返回 (errors, warnings)。"""
    errors, warnings = [], []
    root = mods_dir(sd)
    if not root or not root.is_dir():
        return errors, warnings
    pid = active_pack_id(sd)
    pdir = pack_dir(pid, sd=sd, required=False)

    for p in _files(sd):
        rel = p.relative_to(root).as_posix()
        try:
            clean_rel(rel)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            errors.append(f"mods/{rel} 超过 {MAX_FILE_BYTES // 1024}KB")
        if not read_text(p).strip():
            errors.append(f"mods/{rel} 是空文件")
        if rel.startswith("data/"):
            try:
                json.loads(read_text(p))
            except Exception as exc:
                errors.append(f"mods/{rel} 不是合法 JSON：{exc}")
        if rel.startswith("commands/") and "$ARGUMENTS" not in read_text(p):
            warnings.append(f"mods/{rel} 没有使用 $ARGUMENTS，玩家的输入不会传给命令")

    meta, err = _mod_yaml(sd)
    if err:
        errors.append(f"mods/pack.yaml：{err}")
    custom_ui = meta.get("ui_schema")
    if custom_ui is not None and not isinstance(custom_ui, dict):
        errors.append("mods/pack.yaml 的 ui_schema 必须是字典")
        custom_ui = None
    if custom_ui:
        rules, _ = safe_load_yaml(Path(sd) / "world" / "rules.yaml")
        check_ui_schema(custom_ui, schema_map(rules or {}), errors, warnings)

    theme_path = root / "theme.json"
    if theme_path.exists():
        try:
            raw_theme = json.loads(read_text(theme_path))
            if not isinstance(raw_theme, dict):
                raise ValueError("顶层必须是 JSON 对象")
            check_theme_data(load_theme(sd, pid), errors, warnings)
        except ValueError as exc:
            errors.append(f"mods/theme.json：{exc}")

    def panel_exists(rel_file):
        return (root / rel_file).is_file() or bool(pdir and (pdir / rel_file).is_file())

    panels = load_ui_schema(sd, pid).get("panels") or []
    check_panels(panels, panel_exists, errors, warnings)
    return errors, warnings


def _transaction(sd, change, label):
    """执行改动并校验；有错误就恢复原样并抛出 ValueError，成功则提交 [tuner] 快照。"""
    snap = snapshot(sd)
    try:
        change()
        errors, warnings = check_mods(sd)
    except Exception:
        restore_snapshot(sd, snap)
        raise
    if errors:
        restore_snapshot(sd, snap)
        log_event("mods.rollback", lvl="WARN", save=Path(sd).name, errors=errors)
        raise ValueError("定制未通过校验，已恢复原样：\n" + "\n".join(errors))
    git_turn_checkpoint(Path(sd), label, prefix="[tuner]")
    log_event("mods.updated", save=Path(sd).name, label=label)
    return warnings


def _write(sd, rel, content):
    target = mods_dir(sd) / clean_rel(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if rel == "theme.json":
        content = json.dumps(normalize_theme_data(json.loads(content)), ensure_ascii=False, indent=2) + "\n"
    target.write_text(str(content), encoding="utf-8")


def _merge_panels(sd, panels):
    meta, _ = _mod_yaml(sd)
    ui = dict(meta.get("ui_schema") or {})
    merged = deep_merge({"panels": ui.get("panels") or []}, {"panels": panels})["panels"]
    ui["panels"] = merged
    meta["ui_schema"] = ui
    (mods_dir(sd) / "pack.yaml").parent.mkdir(parents=True, exist_ok=True)
    (mods_dir(sd) / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_files(sd, files=None, panels=None, label="玩家定制"):
    """一次写入多个 mods 文件并声明面板。panels 按 id 合并进 mods/pack.yaml。"""
    files = files or {}
    if not isinstance(files, dict):
        raise ValueError("files 必须是 路径 -> 内容 的字典")

    def change():
        for rel, content in files.items():
            _write(sd, clean_rel(rel), content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
        if panels:
            _merge_panels(sd, panels)

    return _transaction(sd, change, label)


def apply_ui_patch(sd, theme_patch=None, ui_patch=None, label="定制界面"):
    def change():
        if theme_patch:
            path = mods_dir(sd) / "theme.json"
            current = json.loads(read_text(path)) if path.exists() else {}
            _write(sd, "theme.json", json.dumps(deep_merge(current, theme_patch), ensure_ascii=False))
        if ui_patch:
            meta, _ = _mod_yaml(sd)
            meta["ui_schema"] = deep_merge(meta.get("ui_schema") or {}, ui_patch)
            path = mods_dir(sd) / "pack.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return _transaction(sd, change, label)


def append_doc(sd, doc, text, label="补充设定"):
    if doc not in DOC_FILES:
        raise ValueError(f"只能补充这些文档：{list(DOC_FILES)}")

    def change():
        path = mods_dir(sd) / doc
        path.parent.mkdir(parents=True, exist_ok=True)
        current = read_text(path).rstrip() + "\n\n" if path.exists() else ""
        path.write_text(current + text.strip() + "\n", encoding="utf-8")

    return _transaction(sd, change, label)


def remove_mod(sd, rel):
    """移除一项定制。删面板时连带它的声明和数据一起删。"""
    rel = clean_rel(rel)
    root = mods_dir(sd)
    if not (root / rel).is_file():
        raise ValueError(f"定制不存在：{rel}")

    def change():
        (root / rel).unlink()
        match = re.fullmatch(r"panels/(.+)\.html", rel)
        if match:
            data_file = root / "data" / f"{match.group(1)}.json"
            if data_file.exists():
                data_file.unlink()
            meta, _ = _mod_yaml(sd)
            ui = meta.get("ui_schema") or {}
            if ui.get("panels"):
                ui["panels"] = [p for p in ui["panels"] if str(p.get("id")) != match.group(1)]
                if not ui["panels"]:
                    ui.pop("panels")
                if ui:
                    (root / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
                else:
                    (root / "pack.yaml").unlink()
        for folder in ("panels", "data", "commands"):
            if (root / folder).is_dir() and not any((root / folder).iterdir()):
                (root / folder).rmdir()

    return _transaction(sd, change, f"移除定制 {rel}")


def panel_file(sd, panel_id, pack_id=None):
    """找到面板文件：存档 mods 优先，其次世界包。"""
    if not re.fullmatch(ID_RE, str(panel_id or "")):
        raise ValueError("面板 id 不合法")
    pid = pack_id or active_pack_id(sd)
    panels = load_ui_schema(sd, pid).get("panels") or []
    entry = next((p for p in panels if str(p.get("id")) == str(panel_id)), None)
    if not entry:
        raise ValueError(f"面板不存在：{panel_id}")
    rel = str(entry.get("file") or f"panels/{panel_id}.html")
    if not re.fullmatch(rf"panels/{ID_RE}\.html", rel):
        raise ValueError(f"面板文件路径不合法：{rel}")
    candidates = [mods_dir(sd) / rel] if sd else []
    pdir = pack_dir(pid, sd=sd, required=False)
    if pdir:
        candidates.append(pdir / rel)
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError(f"面板文件不存在：{rel}")


def panel_source(sd, panel_id):
    return read_text(panel_file(sd, panel_id))


def panel_data(sd, panel_id):
    path = mods_dir(sd) / "data" / f"{panel_id}.json" if sd else None
    if not path or not path.is_file():
        return None
    try:
        return json.loads(read_text(path))
    except Exception:
        return None


def record_request(summary, detail="", save="", pack=""):
    """AI 发现引擎现有能力做不到时记一笔，供引擎开发者决定要不要做成通用能力。"""
    REQUESTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "summary": str(summary or "").strip()[:300],
        "detail": str(detail or "").strip()[:2000],
        "save": save,
        "pack": pack,
    }
    with open(REQUESTS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def main():
    ap = argparse.ArgumentParser(description="存档定制层工具")
    ap.add_argument("--save", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    rm = sub.add_parser("remove")
    rm.add_argument("path")
    sub.add_parser("requests")
    args = ap.parse_args()

    if args.cmd == "requests":
        print(read_text(REQUESTS_LOG) if REQUESTS_LOG.exists() else "（还没有记录）")
        return 0
    sd = save_dir(args.save)
    if args.cmd == "list":
        for item in list_mods(sd):
            print(f"{item['kind']}  {item['label']}  mods/{item['path']}")
        return 0
    if args.cmd == "remove":
        try:
            remove_mod(sd, args.path)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"已移除 mods/{args.path}")
        return 0
    errors, warnings = check_mods(sd)
    for w in warnings:
        print(f"  ! {w}")
    for e in errors:
        print(f"  ✗ {e}")
    print("✓ 定制校验通过" if not errors else f"发现 {len(errors)} 个错误")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
