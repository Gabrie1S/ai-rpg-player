"""新建或切换世界存档（支持指定任意题材剧本包 --pack）。

  python engine/tools/newsave.py 霓虹之影                   使用当前默认题材包新建存档并初始化 git
  python engine/tools/newsave.py 霓虹之影 --pack cyberpunk  指定题材包新建存档
  python engine/tools/newsave.py --use 霓虹之影             切换当前存档
  python engine/tools/newsave.py --list                     列出所有存档及其所属题材包
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    GIT_IDENTITY,
    SAVES,
    TEMPLATE_WORLD,
    active_pack_id,
    child_directory,
    current_save_name,
    load_ui_schema,
    load_world,
    pack_dir,
    safe_load_yaml,
    save_dir,
)
from pack_setup import apply_setup, load_setup, resolve_answers  # noqa: E402


def set_current(name):
    save_dir(name)
    SAVES.mkdir(parents=True, exist_ok=True)
    (SAVES / "current.txt").write_text(name + "\n", encoding="utf-8")
    print(f"当前存档：{name}")


def overlay_tree(src, dst):
    """将 src 目录下的文件覆盖/合并到 dst 目录。"""
    if not src or not src.is_dir():
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def build_initial_journal(ui_schema):
    """根据世界观包的 ui_schema.journal_sections 生成初始角色日志卡结构。"""
    sections = (ui_schema or {}).get("journal_sections") or []
    lines = [
        "# 角色日志",
        "",
        "角色本人知道的事。台前主持人照这里说话，不说出角色不该知道的。",
        "",
    ]
    for sec in sections:
        title = sec.get("title") if isinstance(sec, dict) else str(sec)
        hint = sec.get("hint", "") if isinstance(sec, dict) else ""
        if title:
            lines.append(f"## {title}")
            if hint:
                lines.append(f"<!-- {hint} -->")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def create_save(name, pack_id=None, setup_answers=None, make_current=True):
    """新建存档。传了 setup_answers（来自开局表单）时检查必答项，不传时只写入作者设定的值。"""
    clean_name = (name or "").strip()
    dest = child_directory(SAVES, clean_name)
    SAVES.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise ValueError(f"存档已存在：{dest}")

    pid = (pack_id or active_pack_id() or "").strip()
    if not pid:
        raise ValueError("当前尚无可用的世界观包，请先通过创世向导构想世界或将世界包拖入 packs/ 目录")
    pdir = pack_dir(pid, required=False)
    if not pdir or not pdir.is_dir():
        raise ValueError(f"世界观包不存在：packs/{pid}")
    if not (pdir / "pack.yaml").exists() or not (pdir / "rules.yaml").exists():
        raise ValueError(f"世界观包「{pid}」尚未完成解析（缺少 pack.yaml 或 rules.yaml），请先在创世向导或 Pack Hub 中完成解析")

    pack_meta, _ = safe_load_yaml(pdir / "pack.yaml")
    pack_meta = pack_meta or {}
    setup_items = load_setup(pdir)
    setup_values = resolve_answers(setup_items, setup_answers, strict=setup_answers is not None)

    # 1. 先复制通用空世界骨架
    shutil.copytree(TEMPLATE_WORLD, dest)
    # 2. 叠加题材包自带的 templates/world（如有）
    overlay_tree(pdir / "templates" / "world", dest)
    # 3. 同步题材包根目录的 rules.yaml 到存档 world/rules.yaml
    if (pdir / "rules.yaml").exists():
        shutil.copy2(pdir / "rules.yaml", dest / "world" / "rules.yaml")

    wy = dest / "world" / "world.yaml"
    world, _ = safe_load_yaml(wy)
    world = world or {}
    world["name"] = clean_name
    world["pack"] = pid
    if pack_meta.get("genre"):
        world["genre"] = pack_meta["genre"]
    if pack_meta.get("calendar"):
        world["calendar"] = pack_meta["calendar"]
    wy.write_text(yaml.safe_dump(world, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 4. 若题材包未提供自定义 journal.md，则根据 pack.yaml 的 ui_schema.journal_sections 初始化日志卡分栏
    if not (pdir / "templates" / "world" / "player" / "journal.md").exists():
        ui_schema = load_ui_schema(dest, pid)
        (dest / "player" / "journal.md").write_text(build_initial_journal(ui_schema), encoding="utf-8")

    apply_setup(dest, setup_items, setup_values)

    try:
        from index import build
        build(dest)
    except Exception:
        pass
    print(f"已新建存档：{dest}（题材包：{pid}）")
    if make_current:
        set_current(clean_name)

    if shutil.which("git"):
        try:
            subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
            subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "第0天：新建世界"], cwd=dest, check=True)
            print("已初始化存档 git 仓库")
        except subprocess.CalledProcessError as e:
            print(f"git 初始化失败（可稍后手动 git init）：{e}")
    return dest


def main():
    p = argparse.ArgumentParser(description="新建或切换存档")
    p.add_argument("name", nargs="?")
    p.add_argument("--pack", help="指定题材包 ID（对应 packs/<id>/），默认读取 config.yaml")
    p.add_argument("--use", metavar="NAME", help="切换当前存档")
    p.add_argument("--list", action="store_true", help="列出所有存档")
    a = p.parse_args()
    SAVES.mkdir(parents=True, exist_ok=True)

    if a.list:
        cur = current_save_name()
        saves = [d for d in sorted(SAVES.iterdir()) if d.is_dir() and not (d / "world" / "candidate.yaml").exists()]
        for d in saves:
            w = load_world(d)
            pk = w.get("pack", "未知")
            genre = w.get("genre", "")
            mark = "* " if d.name == cur else "  "
            print(f"{mark}{d.name}  [题材包: {pk}{' · ' + genre if genre else ''}]")
        if not saves:
            print("（还没有存档）")
        return 0
    if a.use:
        if not save_dir(a.use, required=False):
            sys.exit(f"存档不存在：{a.use}")
        set_current(a.use)
        return 0
    if not a.name:
        p.print_help()
        return 0

    try:
        create_save(a.name, a.pack)
    except ValueError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
