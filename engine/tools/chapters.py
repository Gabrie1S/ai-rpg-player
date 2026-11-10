"""章节摘要（log/summaries.md）：每轮发给模型的是摘要，不是全部原文。

- 每满 CHAPTER_TURNS 轮，把还没总结的最早那一段写成一章（主角视角），由模型来写，失败时退回机械摘录。
- 每章带 `<!-- covers:起-止 -->` 标记，记着覆盖到第几轮。没覆盖到的回合由最近回合的原文和一句话摘要补上，中间不留空档。
- 原文永远保留在 log/story/，摘要只是给模型的工作记忆。
"""
import re
from pathlib import Path

from common import read_text

CHAPTER_TURNS = 12
COVER_RE = re.compile(r"<!--\s*covers:(\d+)-(\d+)\s*-->")
HEADER = "# 章节摘要\n\n从主角视角记录的阶段性剧情摘要，不含幕后剧透。\n"
CONTEXT_FULL_CHAPTERS = 3
CONTEXT_MAX_CHARS = 6000


def summaries_path(sd):
    return Path(sd) / "log" / "summaries.md"


def read_chapters(sd, total=None):
    """带覆盖标记的章节。撤回回合后，覆盖到已撤回回合的章节不再算数。"""
    path = summaries_path(sd)
    if not path.is_file():
        return []
    chapters = []
    for part in re.split(r"(?m)^(?=## )", read_text(path))[1:]:
        m = COVER_RE.search(part)
        if not m:
            continue
        start, end = int(m.group(1)), int(m.group(2))
        if total is not None and end > total:
            continue
        lines = [line for line in part.splitlines()[1:] if line.strip() and not COVER_RE.search(line)]
        chapters.append({"start": start, "end": end, "heading": part.splitlines()[0][3:].strip(), "title": lines[0] if lines else "", "text": part.strip()})
    return chapters


def covered_until(sd, total=None):
    """连续覆盖到第几轮（从第 1 轮起不断档）。"""
    end = 0
    for ch in sorted(read_chapters(sd, total), key=lambda c: c["start"]):
        if ch["start"] <= end + 1:
            end = max(end, ch["end"])
    return end


def next_chapter_range(sd, total, keep_recent=3):
    """该写新一章时返回（起，止）。最近几轮原文本来就在上下文里，不急着总结。"""
    covered = covered_until(sd, total)
    if total - covered >= CHAPTER_TURNS + keep_recent:
        return covered + 1, covered + CHAPTER_TURNS
    return None


def mechanical_chapter(turns):
    """模型写不了时的退路：每轮一句（行动 → 结尾），至少不断档。"""
    lines = []
    for t in turns:
        paras = [p.strip() for p in (t.get("story") or "").split("\n\n") if p.strip() and not p.strip().startswith("【难关】")]
        act = (t.get("input") or "继续").replace("\n", " ")[:48]
        lines.append(f"- 第 {t['index']} 轮（{act}）→ {(paras[-1].replace(chr(10), ' ')[:95]) if paras else ''}")
    return "（这一章是自动摘录）", "\n".join(lines)


def append_chapter(sd, turns, title, body):
    start, end = turns[0]["index"], turns[-1]["index"]
    span = f"{turns[0].get('date_header', '')} 至 {turns[-1].get('date_header', '')}"
    path = summaries_path(sd)
    existing = read_text(path).rstrip() if path.exists() else HEADER.rstrip()
    section = f"\n\n## 第 {start}~{end} 轮 · {span}\n<!-- covers:{start}-{end} -->\n\n{title.strip()}\n\n{body.strip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + section, encoding="utf-8")


SUMMARY_PROMPT = """下面是一局文字跑团第 {start}~{end} 轮的原文。把它写成一章摘要，供主持人以后回顾前情。

要求：
- 第一行写一句话标题，然后空一行写正文，300～600 字。
- 用主角视角，按时间顺序写清：发生了什么、主角做了哪些选择、和谁结下了什么关系或约定、受了什么伤得了什么东西、还有哪些事没了结。
- 人名、地名、物品名照原文写。只写主角知道的，不猜测幕后真相，不评价，不写升华。
- 只输出摘要本身，不要任何说明。

{text}"""


def summary_prompt(turns):
    text = "\n\n---\n\n".join(f"第 {t['index']} 轮 · {t.get('date_header', '')}\n玩家：{t.get('input') or '（继续）'}\n\n{t.get('story', '')}" for t in turns)
    return SUMMARY_PROMPT.format(start=turns[0]["index"], end=turns[-1]["index"], text=text)


def split_summary(text):
    lines = [line for line in (text or "").strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "", ""
    return lines[0].strip().lstrip("#").strip(), "\n".join(lines[1:]).strip()


def context_block(sd, total):
    """放进每轮上下文的前情：最近几章全文，更早的只留标题。总长有上限，玩得再久也不会越来越大。"""
    chapters = sorted(read_chapters(sd, total), key=lambda c: c["start"])
    if not chapters:
        return ""
    recent = chapters[-CONTEXT_FULL_CHAPTERS:]
    older = chapters[:-CONTEXT_FULL_CHAPTERS]
    parts = []
    if older:
        parts.append("更早的章节（只列标题）：\n" + "\n".join(f"- {c['heading']}：{c['title']}" for c in older))
    parts.extend(re.sub(r"\n?<!--.*?-->\n?", "\n", c["text"]).strip() for c in recent)
    block = "\n\n".join(parts)
    if len(block) > CONTEXT_MAX_CHARS:
        block = "（更早的标题已省略）\n\n" + block[-CONTEXT_MAX_CHARS:]
    return block
