"""读取存档的故事原文（log/story/）。原文永不删改，只按需读。

故事越玩越长，每次刷新界面、每轮装上下文都要用到。按文件的修改时间缓存解析结果，
只有新写入的那个文件会重新解析。
"""
import re
from pathlib import Path

from common import read_text

_CACHE = {}


def _parse_file(f, volume):
    raw = read_text(f).strip()
    if not raw:
        return []
    lines = raw.splitlines()
    header = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else f.stem
    turns = []
    for idx, block in enumerate(re.split(r"(?m)^---[ \t]*$", raw)):
        block = block.strip()
        if not block or block.startswith("# "):
            block = "\n".join(line for line in block.splitlines() if not line.startswith("# ")).strip()
            if not block:
                continue
        said, prose = "", block
        if block.startswith("**你：**"):
            parts = block.split("\n\n", 2)
            if len(parts) >= 3:
                said = "\n".join(line.lstrip("> ").strip() for line in parts[1].splitlines())
                prose = parts[2].strip()
            elif len(parts) == 2:
                prose = parts[1].strip()
        if prose:
            turns.append({"block_index": idx, "volume": volume, "date_header": header, "input": said, "story": prose})
    return turns


def _file_turns(sd, f, volume):
    stat = f.stat()
    key, sig = str(f), (stat.st_mtime_ns, stat.st_size)
    hit = _CACHE.get(key)
    if not hit or hit[0] != sig:
        rel = str(f.relative_to(sd)).replace("\\", "/")
        hit = (sig, [{**t, "file": rel} for t in _parse_file(f, volume)])
        _CACHE[key] = hit
    return hit[1]


def all_turns(sd):
    """全部回合，按时间顺序，`index` 从 1 开始。"""
    story_dir = Path(sd) / "log" / "story"
    if not story_dir.is_dir():
        return []
    out = []
    for vol_dir in sorted(story_dir.iterdir()):
        if vol_dir.is_dir():
            for f in sorted(vol_dir.glob("*.md")):
                out.extend(_file_turns(Path(sd), f, vol_dir.name))
    return [{**t, "index": i} for i, t in enumerate(out, 1)]


def story_turns(sd, limit=30, before=None):
    """最近 limit 轮；给了 before 时只取编号小于它的（往前翻页用）。"""
    turns = all_turns(sd)
    if before:
        turns = [t for t in turns if t["index"] < int(before)]
    return turns[-limit:] if limit else turns


def count_turns(sd):
    return len(all_turns(sd))
