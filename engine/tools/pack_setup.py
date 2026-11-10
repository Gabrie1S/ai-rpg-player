"""世界包的开局设置清单（pack.yaml 的 setup）。

世界包可以完整也可以留白，所以按项声明每一件事由谁决定：
  fixed    作者定死，不问玩家
  default  有默认值，玩家可以改
  ask      开局时问玩家（required: false 时可留空交给 AI）

保留项 id 为 ui 的条目不是提问，而是声明要不要进入定制界面这一步。
玩家的回答只写进存档，世界包本身不变，所以同一个包可以反复开新局。

  python engine/tools/pack_setup.py show <世界包ID>
  python engine/tools/pack_setup.py apply <存档名> --json '{"name": "乘风"}'
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from common import load_world, pack_dir, read_frontmatter, read_text, safe_load_yaml, save_dir, write_frontmatter  # noqa: E402

VALID_MODES = ("ask", "default", "fixed")
VALID_TYPES = ("text", "textarea", "choice")
UI_SLOT = "ui"
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CANON_START = "<!-- setup:start -->"
CANON_END = "<!-- setup:end -->"


def load_setup(pdir):
    """读取世界包的 setup 原始条目；没有声明时返回空列表。"""
    if not pdir or not (Path(pdir) / "pack.yaml").exists():
        return []
    meta, err = safe_load_yaml(Path(pdir) / "pack.yaml")
    raw = (meta or {}).get("setup") if not err and isinstance(meta, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _questions(items):
    return [item for item in items if item.get("id") != UI_SLOT]


def _is_required(item):
    return item.get("mode", "ask") == "ask" and item.get("required", True) is not False


def _valid_target(target):
    if target in ("canon", "note"):
        return True
    return bool(re.fullmatch(r"sheet\.[A-Za-z0-9_]+", str(target)))


def check_setup(raw):
    """体检用：返回 setup 清单里的错误。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["pack.yaml setup 应为列表"]
    errors, seen = [], set()
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            errors.append(f"setup 第 {idx} 项应为字典")
            continue
        item_id = str(item.get("id") or "")
        where = f"setup「{item_id or idx}」"
        if not ID_RE.match(item_id):
            errors.append(f"{where} 的 id 只能用字母、数字、下划线和连字符")
        if item_id in seen:
            errors.append(f"{where} 的 id 重复")
        seen.add(item_id)
        mode = item.get("mode", "ask")
        if mode not in VALID_MODES:
            errors.append(f"{where} 的 mode 应为 {list(VALID_MODES)}")
        if item_id == UI_SLOT:
            continue
        if mode != "fixed" and not item.get("label"):
            errors.append(f"{where} 要问玩家，必须写 label")
        item_type = item.get("type", "text")
        if item_type not in VALID_TYPES:
            errors.append(f"{where} 的 type 应为 {list(VALID_TYPES)}")
        if item_type == "choice" and not (isinstance(item.get("options"), list) and item["options"]):
            errors.append(f"{where} 是选择题，必须写 options")
        if mode in ("fixed", "default") and item.get("value") in (None, ""):
            errors.append(f"{where} 的 mode 是 {mode}，必须写 value")
        if item_type == "choice" and item.get("value") not in (None, "") and item.get("value") not in (item.get("options") or []):
            errors.append(f"{where} 的 value 不在 options 里")
        if not _valid_target(item.get("target", "canon")):
            errors.append(f"{where} 的 target 应为 canon、note 或 sheet.<字段名>")
    return errors


def public_setup(items):
    """给前端的提问表单。作者定死的项不出现，玩家也就看不到它的值。"""
    out = []
    for item in _questions(items):
        mode = item.get("mode", "ask")
        if mode == "fixed":
            continue
        out.append({
            "id": item.get("id"),
            "label": item.get("label", ""),
            "mode": mode,
            "type": item.get("type", "text"),
            "options": item.get("options") or [],
            "value": item.get("value", ""),
            "hint": item.get("hint", ""),
            "required": _is_required(item),
        })
    return out


def ui_mode(items, theme=None):
    """定制界面这一步怎么走：ask 进入这一步；default 可选；fixed 不打扰玩家。"""
    for item in items:
        if item.get("id") == UI_SLOT:
            mode = item.get("mode", "ask")
            return mode if mode in VALID_MODES else "ask"
    has_design = isinstance(theme, dict) and (theme.get("dialogue_box") or theme.get("pixel_frame"))
    return "default" if has_design else "ask"


def resolve_answers(items, answers=None, strict=False):
    """合并作者设定和玩家回答，返回 {id: 值}。strict 时必答项缺失或选项不合法会报错。"""
    answers = answers if isinstance(answers, dict) else {}
    values, missing = {}, []
    for item in _questions(items):
        item_id = item.get("id")
        mode = item.get("mode", "ask")
        default = item.get("value", "")
        given = str(answers.get(item_id) or "").strip()
        value = default if mode == "fixed" else (given or default)
        value = str(value).strip() if value is not None else ""
        if given and mode != "fixed" and item.get("type") == "choice" and given not in [str(o) for o in item.get("options") or []]:
            raise ValueError(f"「{item.get('label', item_id)}」的选择不在可选范围内：{given}")
        if value:
            values[item_id] = value
        elif strict and _is_required(item):
            missing.append(str(item.get("label", item_id)))
    if missing:
        raise ValueError(f"开局还需要填写：{'、'.join(missing)}")
    return values


def _canon_block(lines):
    return f"{CANON_START}\n## 开局选择\n\n" + "\n".join(lines) + f"\n{CANON_END}"


def _write_canon(sd, lines):
    path = Path(sd) / "world" / "canon.md"
    existing = read_text(path) if path.exists() else "# 玩家亲定事实\n"
    pattern = re.compile(re.escape(CANON_START) + r".*?" + re.escape(CANON_END), re.S)
    cleaned = pattern.sub("", existing).rstrip()
    path.write_text(f"{cleaned}\n\n{_canon_block(lines)}\n" if lines else f"{cleaned}\n", encoding="utf-8")


def apply_setup(sd, items, values):
    """把设置结果写进存档。可以重复调用，结果相同。"""
    sd = Path(sd)
    canon_lines, sheet_fields = [], {}
    by_id = {item.get("id"): item for item in _questions(items)}
    for item_id, value in values.items():
        item = by_id.get(item_id)
        if not item:
            continue
        target = str(item.get("target", "canon"))
        if target == "canon":
            canon_lines.append(f"- {item.get('label', item_id)}：{value}")
        elif target.startswith("sheet."):
            sheet_fields[target.split(".", 1)[1]] = value

    if sheet_fields:
        sheet_path = sd / "player" / "sheet.md"
        meta, body = read_frontmatter(sheet_path)
        write_frontmatter(sheet_path, {**(meta or {}), **sheet_fields}, body)
    _write_canon(sd, canon_lines)
    if values:
        (sd / "world" / "setup.yaml").write_text(yaml.safe_dump(values, allow_unicode=True, sort_keys=False), encoding="utf-8")


def describe_for_seed(items, values):
    """给幕后生成世界种子时看的说明。"""
    lines = []
    for item in _questions(items):
        item_id = item.get("id")
        label = item.get("label", item_id)
        if item_id in values:
            lines.append(f"- {label}：{values[item_id]}")
        elif item.get("mode", "ask") == "ask":
            lines.append(f"- {label}：玩家交给你决定")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="世界包开局设置清单")
    sub = ap.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="显示要问玩家的项")
    show.add_argument("pack_id")
    apply_p = sub.add_parser("apply", help="把玩家回答写进存档")
    apply_p.add_argument("save")
    apply_p.add_argument("--json", default="{}", help="玩家回答，如 {\"name\": \"乘风\"}")
    args = ap.parse_args()

    if args.cmd == "show":
        items = load_setup(pack_dir(args.pack_id, required=True))
        print(json.dumps(public_setup(items), ensure_ascii=False, indent=2))
        return 0
    sd = save_dir(args.save)
    pack_id = load_world(sd).get("pack")
    items = load_setup(pack_dir(pack_id, sd=sd, required=False))
    try:
        values = resolve_answers(items, json.loads(args.json), strict=True)
    except ValueError as exc:
        sys.exit(str(exc))
    apply_setup(sd, items, values)
    print(f"已写入存档「{sd.name}」：{list(values)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
