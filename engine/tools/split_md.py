"""把一个大的 markdown 文件按二级标题（## ）拆成文件夹里的多个小文件并生成目录；过长的章节继续拆到不超过 12000 字。

  python engine/tools/split_md.py <大文件> <目标文件夹>
  python engine/tools/split_md.py --index <目标文件夹>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import read_text  # noqa: E402

MAX_SLICE_CHARS = 12000
BAD = r'[\\/:*?"<>|\r\n\t]'


def safe_name(title):
    t = re.sub(BAD, "", title).strip().strip(".")
    t = re.sub(r"\s+", " ", t)
    return t[:24] or "未命名"


def gist(body):
    for line in body.split("\n"):
        l = line.strip()
        if not l or l.startswith("#") or l.startswith("<!--") or set(l) <= set("|-: "):
            continue
        l = re.sub(r"[*`>|]", "", l).strip("- ").strip()
        if l:
            return (l[:40] + "…") if len(l) > 40 else l
    return ""


def _pack_chunks(pieces, joiner, limit):
    chunks, cur = [], ""
    for piece in pieces:
        candidate = f"{cur}{joiner}{piece}" if cur else piece
        if cur and len(candidate) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = candidate
    return chunks + [cur] if cur else chunks


def chunk_body(body, limit=MAX_SLICE_CHARS):
    """把超长章节继续拆小：优先按三级标题，其次按段落，最后才硬切。"""
    if len(body) <= limit:
        return [body]
    pieces = re.split(r"(?m)^(?=### )", body)
    joiner = ""
    if len(pieces) < 2:
        pieces = re.split(r"\n\s*\n", body)
        joiner = "\n\n"
    out = []
    for chunk in _pack_chunks([p.strip() if joiner else p for p in pieces if p.strip()], joiner, limit):
        if len(chunk) <= limit:
            out.append(chunk.strip())
        elif joiner:
            out.extend(chunk[i : i + limit] for i in range(0, len(chunk), limit))
        else:
            out.extend(chunk_body(chunk.strip(), limit))
    return out


def split(src, dest, overview_name="00-总览.md"):
    """拆分 src 到 dest，返回写出的文件名列表。不会覆盖 dest 中已有的其他文件（src 自身除外）。"""
    src, dest = Path(src), Path(dest)
    text = read_text(src)
    parts = re.split(r"(?m)^## ", text)
    dest.mkdir(parents=True, exist_ok=True)
    sections = []
    head_body = re.sub(r"(?m)^# .*\n?", "", parts[0], count=1).strip()
    if head_body:
        sections.append((Path(overview_name).stem, "总览", head_body))
    for i, part in enumerate(parts[1:], 1):
        title, _, body = part.partition("\n")
        title = title.strip()
        sections.append((f"{i:02d}-{safe_name(title)}", title, body.strip()))

    written = []
    for stem, title, body in sections:
        chunks = chunk_body(body)
        for n, chunk in enumerate(chunks, 1):
            multi = len(chunks) > 1
            target = dest / (f"{stem}-{n}.md" if multi else f"{stem}.md")
            serial = 2
            while target.exists() and target.resolve() != src.resolve() and target.name not in written:
                target = dest / f"{target.stem}_{serial}.md"
                serial += 1
            shown = f"{title}（{n}/{len(chunks)}）" if multi else title
            target.write_text(f"# {shown}\n\n{chunk}\n", encoding="utf-8")
            written.append(target.name)
    write_index(dest)
    return written


def write_index(folder):
    lines = [f"# 目录：{folder.name}", "", "每个文件一行，后面是第一句正文作为梗概。需要细节时再读对应文件。", ""]
    for f in sorted(folder.glob("*.md")):
        if f.name == "目录.md":
            continue
        body = read_text(f)
        title = body.split("\n", 1)[0].lstrip("# ").strip()
        g = gist(body.split("\n", 1)[1] if "\n" in body else "")
        lines.append(f"- `{f.name}` {title}" + (f"：{g}" if g else "") + f"（{len(body)} 字）")
    (folder / "目录.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--index":
        write_index(Path(sys.argv[2]))
        print("目录已更新")
    elif len(sys.argv) == 3:
        names = split(Path(sys.argv[1]), Path(sys.argv[2]))
        print(f"已拆成 {len(names)} 个文件")
    else:
        print(__doc__)
