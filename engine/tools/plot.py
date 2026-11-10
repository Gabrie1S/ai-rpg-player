"""剧情规划（world/design/剧情规划.md）：剧情按规划走，不靠台前临场编。

幕后维护“当前卷 + 接下来几章”；台前每轮只看当前章；规划不够时引擎在下一轮前让幕后补上。
"""
import re
from pathlib import Path

from common import read_text

PLOT_GLOB = "*剧情规划*.md"
MIN_AHEAD = 2          # 当前章之外至少还要规划几章，少于这个数就让幕后补
BRIEF_MAX_CHARS = 2200


def plot_file(sd):
    folder = Path(sd) / "world" / "design"
    found = sorted(folder.glob(PLOT_GLOB)) if folder.is_dir() else []
    return found[0] if found else None


def _sections(text):
    """[(二级标题, 正文)]，注释去掉。"""
    out = []
    for m in re.finditer(r"(?ms)^## +(.+?)[ \t]*\n(.*?)(?=^## |\Z)", text or ""):
        out.append((m.group(1).strip(), re.sub(r"(?s)<!--.*?-->", "", m.group(2)).strip()))
    return out


def _volume(text):
    """当前卷。认“## 当前卷”“## 当前卷节拍”这类标题，也认正文里的“当前卷：……”一行。"""
    for title, body in _sections(text):
        if title.startswith("当前卷") and body:
            return re.sub(r"^当前卷[：:]\s*", "", body)
    m = re.search(r"(?m)^当前卷[：:]\s*(.+)$", text or "")
    return m.group(1).strip() if m else ""


HEADING_RE = re.compile(r"(?m)^(#{2,3}) +(.+?)[ \t]*$")
CHAPTER_RE = re.compile(r"^第[0-9一二三四五六七八九十百零〇两]+章")


def upcoming(text):
    """还没完成的章：[(标题行, 正文)]。

    模板写的是“## 接下来几章”下面一章一个“### 第N章 · 标题”。但模型（尤其在没有模板的老存档上）
    常换说法：“接下来的章”“进行中的章”，或者干脆每章一个“## 第N章：标题”。所以不看分组标题怎么叫，
    凡是“第N章”开头的二、三级标题都算一章；标题或它所在的分组写着“完成”的不算。
    """
    text = re.sub(r"(?s)<!--.*?-->", "", text or "")
    heads = list(HEADING_RE.finditer(text))
    chapters, group = [], ""
    for i, m in enumerate(heads):
        level, title = len(m.group(1)), m.group(2).strip()
        if level == 2 and not CHAPTER_RE.match(title):
            group = title
            continue
        if not CHAPTER_RE.match(title):
            continue
        if level == 2:
            group = ""
        if "已完成" in title or "完成" in group:
            continue
        end = next((n.start() for n in heads[i + 1:] if len(n.group(1)) <= level), len(text))
        body = text[m.end():end].strip()
        if any(k in group for k in ("进行中", "当前")) and "进行中" not in title:
            title += "（进行中）"
        chapters.append((title, body))
    return chapters


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "world" / "world" / "design"
TEMPLATE_FILES = ("剧情规划.md", "排期总表.md", "节奏备忘.md")


def ensure_templates(sd):
    """老存档（加剧情规划之前建的）没有骨架：缺哪份补哪份，幕后在骨架上填，标题才和引擎对得上。"""
    folder = Path(sd) / "world" / "design"
    folder.mkdir(parents=True, exist_ok=True)
    added = []
    for name in TEMPLATE_FILES:
        src = TEMPLATE_DIR / name
        if src.is_file() and not any(folder.glob(f"*{Path(name).stem}*.md")):
            (folder / name).write_text(read_text(src), encoding="utf-8")
            added.append(name)
    return added


def needs_planning(sd):
    """没有剧情规划，或者当前章之后已经不够 MIN_AHEAD 章了。"""
    path = plot_file(sd)
    if not path:
        return True
    return len(upcoming(read_text(path))) < MIN_AHEAD + 1


def brief(sd):
    """给台前的：当前卷走到哪、当前这一章、后面几章的标题。"""
    path = plot_file(sd)
    if not path:
        return ""
    text = read_text(path)
    volume = _volume(text)
    chapters = upcoming(text)
    if not volume and not chapters:
        return ""
    parts = []
    if volume:
        parts.append("当前卷：" + volume[:600])
    if chapters:
        current = next((c for c in chapters if "进行中" in c[0]), chapters[0])
        parts.append(f"当前章：{current[0]}\n{current[1][:1300]}")
        later = [c[0] for c in chapters if c is not current]
        if later:
            parts.append("之后：" + "；".join(later[:3]))
    return "\n\n".join(parts)[:BRIEF_MAX_CHARS]
