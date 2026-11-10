"""重新生成世界：新内容写进隐藏的候选存档，旧存档全程不动，生成完由玩家决定用哪个。

候选存档是一个普通的存档目录（驱动、校验都能直接用），带 `world/candidate.yaml` 标记，
不进存档列表。标记不进存档快照。
"""
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

from common import GIT_IDENTITY, SAVES, child_directory, load_world

MARKER = "world/candidate.yaml"


def clean_save_name(name):
    """存档名就是目录名，不能带路径。"""
    clean = (name or "").strip()
    child_directory(SAVES, clean)
    return clean


def replaces_of(sd):
    """这个目录是候选存档时，返回它要替换的存档名；否则返回空字符串。"""
    path = Path(sd) / MARKER
    if not path.is_file():
        return ""
    try:
        meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return clean_save_name(meta.get("replaces")) if isinstance(meta, dict) else ""
    except Exception:
        return ""


def is_candidate(sd):
    return (Path(sd) / MARKER).is_file()


def list_candidates():
    if not SAVES.is_dir():
        return []
    return [d for d in sorted(SAVES.iterdir()) if d.is_dir() and not d.is_symlink() and replaces_of(d)]


def find_for(name):
    return next((d for d in list_candidates() if replaces_of(d) == name), None)


def free_name(base):
    """base 没被占用就用 base，否则依次试 base 后面加 2、3……"""
    return numbered_name(base, "")


def numbered_name(prefix, suffix, start=None):
    """按「前缀 + 序号 + 后缀」找一个没被占用的目录名。start 为空时先试不带序号的名字。"""
    number = start
    while True:
        name = f"{prefix}{number or ''}{suffix}"
        if not (SAVES / name).exists():
            return name
        number = (number or 1) + 1


def has_world(sd):
    """这一局有没有不能随便覆盖的内容：已有世界实体、玩过，或者生成完成过。"""
    sd = Path(sd)
    world = sd / "world"
    if any(any((world / kind).glob("*.md")) for kind in ("factions", "locations", "npcs")):
        return True
    story = sd / "log" / "story"
    if story.is_dir() and any(story.rglob("*.md")):
        return True
    if not (sd / ".git").is_dir() or not shutil.which("git"):
        return False
    out = subprocess.run(
        ["git", "-C", str(sd), "log", "--format=%s", "--fixed-strings", "--grep=[seed]", "--grep=[turn]", "-1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return bool(out.stdout.strip())


def set_world_name(sd, name):
    """改 world.yaml 里的名字，只提交这一个文件，不把玩家其他没提交的改动扫进去。"""
    sd = Path(sd)
    path = sd / "world" / "world.yaml"
    world = load_world(sd)
    if world.get("name") == name:
        return
    world["name"] = name
    path.write_text(yaml.safe_dump(world, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if (sd / ".git").is_dir() and shutil.which("git"):
        subprocess.run(
            ["git", *GIT_IDENTITY, "-C", str(sd), "commit", "-q", "-m", f"[sync] 世界改名：{name}", "--", "world/world.yaml"],
            capture_output=True,
        )


def mark(sd, replaces):
    sd = Path(sd)
    (sd / MARKER).write_text(yaml.safe_dump({"replaces": replaces}, allow_unicode=True), encoding="utf-8")


def _force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def discard(sd):
    shutil.rmtree(sd, onerror=_force_remove)


def _rename(src, dst):
    try:
        Path(src).rename(dst)
    except OSError as exc:
        raise ValueError(f"「{Path(src).name}」的文件正被别的程序占用，没能切换。关掉占用它的程序后再试。") from exc


def adopt(sd):
    """用新的：候选接替原来的名字，旧存档改名留作备份。返回 (新存档名, 备份名)。"""
    sd = Path(sd)
    name = replaces_of(sd)
    target = child_directory(SAVES, name)
    backup = ""
    if target.exists():
        backup = numbered_name(f"{name}（旧版", "）")
        _rename(target, SAVES / backup)
    try:
        _rename(sd, target)
    except ValueError:
        if backup:
            (SAVES / backup).rename(target)
        raise
    (target / MARKER).unlink()
    set_world_name(target, name)
    if backup:
        set_world_name(SAVES / backup, backup)
    return name, backup


def keep_both(sd):
    """都留下：候选另存为一局。返回新存档名。"""
    sd = Path(sd)
    replaces = clean_save_name(replaces_of(sd))
    name = numbered_name(f"{replaces}（第", "版）", start=2)
    _rename(sd, SAVES / name)
    (SAVES / name / MARKER).unlink()
    set_world_name(SAVES / name, name)
    return name
