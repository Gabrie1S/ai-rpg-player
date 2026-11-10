"""原子化状态回写、回合 Git 快照、难关胜率追踪与故事原文直接归档工具。

支持三种输入方式（跨平台兼容 Bash 与 Windows PowerShell）：
  1. 标准输入：python engine/tools/apply.py <<'EOF' [...] EOF
  2. 文件输入：python engine/tools/apply.py --file ops.json
  3. 参数输入：python engine/tools/apply.py --json '[{"op": "set", ...}]'

输入 JSON 支持两种结构：
  A. 纯操作数组：[{"op": "set", ...}, {"op": "append", ...}]
  B. 带本轮故事归档的复合结构：
     {
       "ops": [{"op": "set", ...}],
       "turn": {
         "input": "玩家本轮输入的行动",
         "story": "主持人输出的最终正文（将自动提取【难关】结果并直接归档至 log/story/）"
       }
     }

核心保障：
- 内存级原子事务回滚：如果任何一条 op 报错或写完后 validate.py 发现硬错误，自动还原所有被修改的文件，绝不留脏数据。
- 直接归档原文：不再依赖任何外部 CLI 的私有聊天记录，任何模型与平台下 100% 实时落盘。
- 回合级 Git 快照：每次成功回写或归档后自动在存档 Git 仓库打一个轻量快照，支持一键 Undo / Swipe 重演。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_error, log_event  # noqa: E402
from common import GIT_IDENTITY, ROOT, day_to_date, load_world, read_frontmatter, read_text, save_dir, write_frontmatter  # noqa: E402
from validate import validate  # noqa: E402

CN_NUM = "零一二三四五六七八九十"
# 引擎在命令行驱动下跑幕后推演时设置这个环境变量（一个临时文件路径）：
# apply.py 每次回写的成败追加一行 JSON，引擎据此判断改动有没有真正落进存档
WRITE_LEDGER_ENV = "RPG_WRITE_LEDGER"
CHALLENGE_RE = re.compile(r"【难关】[^\n]*?(?:→|->|=>|：|:)\s*(大成功|成功|代价成功|失败|大失败)")
MAX_SLICE_CHARS = 12000


def vol_name(v):
    v = int(v or 1)
    return f"第{CN_NUM[v]}卷" if 0 < v <= 10 else f"第{v}卷"


def safe_path(sd, rel):
    p = (sd / rel).resolve()
    if sd.resolve() not in p.parents:
        raise ValueError(f"路径不在存档内：{rel}")
    return p


def normalize_op(op):
    """兼容不同大模型常见的字段别名（如 path/rel_path、content/value 等），统一转为标准字段。"""
    if not isinstance(op, dict):
        return op
    out = dict(op)
    kind = str(out.get("op") or "").strip()
    if "file" not in out:
        for alias in ("path", "rel_path", "filepath"):
            if out.get(alias):
                out["file"] = out.pop(alias)
                break
    if kind == "set" and "key" not in out and out.get("field"):
        out["key"] = out.pop("field")
    elif kind in ("append", "append_file") and "text" not in out:
        for alias in ("content", "value", "line"):
            if alias in out and out[alias] is not None:
                out["text"] = str(out.pop(alias))
                break
    elif kind == "add_file" and "content" not in out:
        for alias in ("text", "value", "body"):
            if alias in out and out[alias] is not None:
                out["content"] = str(out.pop(alias))
                break
    elif kind == "replace":
        if "old" not in out:
            for alias in ("target", "from", "search"):
                if alias in out and out[alias] is not None:
                    out["old"] = str(out.pop(alias))
                    break
        if "new" not in out:
            for alias in ("replacement", "to", "value"):
                if alias in out and out[alias] is not None:
                    out["new"] = str(out.pop(alias))
                    break
    return out


def do_set(sd, op):
    p = safe_path(sd, op["file"])
    meta, body = read_frontmatter(p)
    if meta is None:
        raise ValueError(f"{op['file']} 没有 YAML 头")
    if meta.get("locked") and not op.get("force_locked"):
        raise ValueError(f"{op['file']} 已锁定，跳过")
    keys = str(op["key"]).split(".")
    cur = meta
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = op["value"]
    write_frontmatter(p, meta, body)


def do_append(sd, op):
    raw_section = op.get("section")
    head = str(raw_section).strip() if raw_section is not None else ""
    if not head:
        return do_append_file(sd, op)
    p = safe_path(sd, op["file"])
    text = read_text(p) if p.exists() else ""
    line = str(op["text"]).rstrip("\n")
    lines = text.split("\n") if text else []
    idx = next((i for i, l in enumerate(lines) if l.strip() == head), None)
    if idx is None and not head.startswith("#"):
        idx = next(
            (i for i, l in enumerate(lines) if l.lstrip().startswith("#") and l.strip().lstrip("#").strip() == head),
            None,
        )
        if idx is not None:
            head = lines[idx].strip()
        else:
            head = f"## {head}"
    if idx is None:
        new = (text.rstrip("\n") + f"\n\n{head}\n\n{line}\n") if text.strip() else f"{head}\n\n{line}\n"
    else:
        level = len(head) - len(head.lstrip("#"))
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            l = lines[j]
            if l.startswith("#") and len(l) - len(l.lstrip("#")) <= level:
                end = j
                break
        k = end
        while k > idx + 1 and not lines[k - 1].strip():
            k -= 1
        lines.insert(k, line)
        new = "\n".join(lines)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")


def _line_matches_tolerant(file_line, old_line):
    fs, os_ = file_line.strip(), old_line.strip()
    if fs == os_:
        return True
    # 兼容 context.py 对超长日志行末尾追加的省略号“…”截断
    if os_.endswith("…") and len(os_) >= 20 and fs.startswith(os_[:-1]):
        return True
    return False


def _replace_unique_line_span(text, old, new):
    """当精确子串因缩进差异或上下文行尾“…”截断匹配不到时，尝试按整行唯一匹配并保留原缩进。
    返回 (new_text_or_None, candidate_count)。仅当全文件恰好唯一命中 1 处时才执行替换。
    """
    has_trailing_nl = old.endswith("\n")
    old_core = old[:-1] if has_trailing_nl else old
    old_lines = old_core.split("\n")
    if not any(l.strip() for l in old_lines):
        return None, 0
    file_lines = text.split("\n")
    m = len(old_lines)
    if m > len(file_lines):
        return None, 0

    matches = [
        idx
        for idx in range(len(file_lines) - m + 1)
        if all(_line_matches_tolerant(file_lines[idx + j], old_lines[j]) for j in range(m))
    ]
    if len(matches) != 1:
        return None, len(matches)

    start = matches[0]
    matched_lines = file_lines[start : start + m]
    if new == "" and has_trailing_nl:
        updated = file_lines[:start] + file_lines[start + m :]
        return "\n".join(updated), 1

    new_core = new[:-1] if (has_trailing_nl and new.endswith("\n")) else new
    new_lines = new_core.split("\n")
    file_indents = [len(l) - len(l.lstrip(" ")) for l in matched_lines if l.strip()]
    old_indents = [len(l) - len(l.lstrip(" ")) for l in old_lines if l.strip()]
    new_indents = [len(l) - len(l.lstrip(" ")) for l in new_lines if l.strip()]
    if file_indents and old_indents and new_indents:
        delta_indent = min(file_indents) - min(old_indents)
        if delta_indent > 0 and min(new_indents) <= min(old_indents):
            pad = " " * delta_indent
            new_lines = [(pad + l if l.strip() else l) for l in new_lines]

    updated = file_lines[:start] + new_lines + file_lines[start + m :]
    return "\n".join(updated), 1


def do_replace(sd, op):
    p = safe_path(sd, op["file"])
    text = read_text(p)
    old, new = str(op["old"]), str(op["new"])
    n = text.count(old)
    if n == 1:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return
    if n == 0:
        replaced, span_hits = _replace_unique_line_span(text, old, new)
        if replaced is not None:
            p.write_text(replaced, encoding="utf-8")
            return
        if span_hits > 1:
            raise ValueError(
                f"{op['file']}：要替换的文字出现了 0 次，必须恰好 1 次（忽略缩进后有 {span_hits} 处相似行，请多带 id 或上下相邻行以唯一确定位置）"
            )
    raise ValueError(f"{op['file']}：要替换的文字出现了 {n} 次，必须恰好 1 次")


def do_add_file(sd, op):
    p = safe_path(sd, op["file"])
    if p.exists():
        raise ValueError(f"{op['file']} 已存在")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(op["content"], encoding="utf-8")


def do_append_file(sd, op):
    p = safe_path(sd, op["file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    text = read_text(p) if p.exists() else ""
    p.write_text((text.rstrip("\n") + "\n" if text.strip() else "") + op["text"].rstrip("\n") + "\n", encoding="utf-8")


def do_set_panel_data(sd, op):
    """更新自定义面板的数据（mods/data/<面板id>.json）。数据玩家可见，不能放隐藏内容。"""
    panel = str(op["panel"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", panel):
        raise ValueError(f"面板 id 不合法：{panel}")
    path = sd / "mods" / "data" / f"{panel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(op["data"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def do_advance_days(sd, op):
    """推进游戏时间。到期事件和幕后推演由引擎在下一轮开始前处理。"""
    days = int(op["days"])
    if days <= 0:
        raise ValueError("advance_days 的天数必须大于 0")
    path = sd / "world" / "now.md"
    meta, body = read_frontmatter(path)
    if (meta or {}).get("frozen"):
        raise ValueError("世界已冻结，不能推进时间")
    meta["day"] = int(meta.get("day", 0)) + days
    write_frontmatter(path, meta, body)


OPS = {
    "advance_days": do_advance_days,
    "set_panel_data": do_set_panel_data,
    "set": do_set,
    "append": do_append,
    "replace": do_replace,
    "add_file": do_add_file,
    "append_file": do_append_file,
}


def record_turn_ledger(sd, challenge_outcomes=None):
    """记录回合时间戳与难关结果队列到 turns.tsv 与 now.md。"""
    try:
        now_path = sd / "world" / "now.md"
        meta, body = read_frontmatter(now_path)
        if meta is None:
            return
        day, vol = int(meta.get("day", 0)), int(meta.get("volume", 1))
        if challenge_outcomes:
            recent = list(meta.get("recent_challenges") or [])
            recent.extend(challenge_outcomes)
            meta["recent_challenges"] = recent[-5:]
            write_frontmatter(now_path, meta, body)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_dir = sd / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "turns.tsv", "a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{day}\t{vol}\n")
    except Exception:
        pass


def archive_story_turn(sd, player_input, story_text):
    """直接将当轮「玩家输入 + 故事正文」归档至 log/story/第X卷/NNNN-日期.md，并更新难关胜率追踪。"""
    story_text = (story_text or "").strip()
    if not story_text:
        return None
    meta, _ = read_frontmatter(sd / "world" / "now.md")
    meta = meta or {}
    day = int(meta.get("day", 0))
    vol = int(meta.get("volume", 1))
    cal = load_world(sd).get("calendar")
    date_str = day_to_date(day, cal)
    rel = Path(vol_name(vol)) / f"{day:04d}-{date_str}.md"
    story_path = sd / "log" / "story" / rel
    story_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"# {date_str}（第 {day + 1} 天）"
    inp = (player_input or "").strip()
    quoted = "\n".join("> " + l for l in inp.splitlines()) if inp else ""
    said = f"**你：**\n\n{quoted}\n\n" if quoted else ""
    entry = ("" if story_path.exists() else header + "\n\n") + f"---\n\n{said}{story_text}\n\n"
    with open(story_path, "a", encoding="utf-8") as fh:
        fh.write(entry)

    outcomes = CHALLENGE_RE.findall(story_text)
    record_turn_ledger(sd, outcomes)
    return str(rel).replace("\\", "/")


def sync_sliced_indexes(sd):
    """自动维护 world/design/ 与 world/secrets/ 的分层切片（超 12KB 自动拆分）并实时刷新 目录.md。"""
    try:
        from split_md import split as split_markdown_file, write_index
    except Exception:
        return
    for sub in ("design", "secrets"):
        folder = sd / "world" / sub
        if not folder.is_dir():
            continue
        for md_file in list(sorted(folder.glob("*.md"))):
            if md_file.name == "目录.md":
                continue
            try:
                body = read_text(md_file)
                if len(body) > MAX_SLICE_CHARS:
                    # 总览按来源命名，避免同一文件夹里两份大文档的总览互相覆盖
                    written = split_markdown_file(md_file, folder, overview_name=f"00-{md_file.stem}-总览.md")
                    if md_file.name not in written:
                        md_file.unlink()
            except Exception:
                pass
        try:
            write_index(folder)
        except Exception:
            pass


def git_turn_checkpoint(sd, label="", prefix="[turn]"):
    """在存档 Git 仓库中创建一个轻量快照，供一键 Undo / Swipe 回滚与历史溯源使用。"""
    if not shutil.which("git") or not (sd / ".git").is_dir():
        return False
    try:
        meta, _ = read_frontmatter(sd / "world" / "now.md")
        day = int((meta or {}).get("day", 0))
        tag_pfx = (prefix or "[turn]").strip()
        msg = f"{tag_pfx} 第{day}天" + (f"：{label[:30]}" if label else "")
        subprocess.run(["git", "-C", str(sd), "add", "-A"], check=True, capture_output=True)
        status = subprocess.run(
            ["git", "-C", str(sd), "status", "--porcelain"],
            check=True, capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        if status:
            subprocess.run(["git", *GIT_IDENTITY, "-C", str(sd), "commit", "-q", "-m", msg], check=True, capture_output=True)
            log_event("git.checkpoint", save=sd.name, prefix=tag_pfx, msg=msg)
            return True
    except Exception as exc:
        log_error("git.checkpoint_err", exc=exc, save=sd.name)
    return False


def apply_operations(sd, ops, turn_data=None, rollback_on_error=True, commit_checkpoint=True, checkpoint_prefix="[turn]", force_locked=False):
    """核心事务执行函数：在内存中备份所有涉及文件，出错或校验失败时自动回滚。

    force_locked=True 表示整批改动都允许改写带 `locked: true` 的文件（仅 /gm 上帝视角使用），
    等同于给每条操作写上 `"force_locked": true`。
    """
    if isinstance(ops, dict):
        if "ops" in ops or "turn" in ops:
            turn_data = turn_data or ops.get("turn")
            ops = ops.get("ops") or []
        else:
            ops = [ops]

    ops = [normalize_op(op) for op in (ops or [])]
    if force_locked:
        ops = [{**op, "force_locked": True} if isinstance(op, dict) else op for op in ops]
    backups = {}      # Path -> bytes | None (None 表示原本不存在，回滚时需删除)
    touched_files = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "advance_days":
            file_rel = "world/now.md"
        elif kind == "set_panel_data" and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", str(op.get("panel") or "")):
            file_rel = f"mods/data/{op['panel']}.json"
        else:
            file_rel = op.get("file")
        if file_rel:
            touched_files.append(str(file_rel))
            try:
                p = safe_path(sd, file_rel)
                if p not in backups:
                    backups[p] = p.read_bytes() if p.exists() else None
            except Exception:
                pass

    done, errors = 0, []
    for i, op in enumerate(ops, 1):
        try:
            OPS[op["op"]](sd, op)
            done += 1
        except KeyError as e:
            errors.append(f"第{i}条：缺少字段或未知 op {e}")
        except Exception as e:
            errors.append(f"第{i}条：{e}")

    rep, _ = validate(sd)
    val_errors = [f"✗ {e}" for e in rep.errors]

    if (errors or val_errors) and rollback_on_error:
        for p, raw_bytes in backups.items():
            try:
                if raw_bytes is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.write_bytes(raw_bytes)
            except Exception:
                pass
        try:
            from index import build
            build(sd)
        except Exception:
            pass
        log_event(
            "apply.rollback",
            lvl="ERR",
            save=sd.name,
            ops_total=len(ops),
            files=touched_files,
            ops=ops,
            errors=errors + val_errors,
            warnings=rep.warnings,
        )
        return {
            "ok": False,
            "done": 0,
            "total": len(ops),
            "rolled_back": True,
            "errors": errors + val_errors,
            "warnings": rep.warnings,
        }

    try:
        from index import build
        build(sd)
    except Exception:
        pass
    sync_sliced_indexes(sd)

    archived_to = None
    if isinstance(turn_data, dict) and turn_data.get("story"):
        archived_to = archive_story_turn(sd, turn_data.get("input", ""), turn_data.get("story", ""))
    elif ops:
        record_turn_ledger(sd)

    if commit_checkpoint and (ops or archived_to):
        label = (turn_data or {}).get("input", "") if isinstance(turn_data, dict) else ""
        git_turn_checkpoint(sd, label, prefix=checkpoint_prefix)

    log_event(
        "apply.commit",
        lvl="WARN" if (errors or val_errors) else "INFO",
        save=sd.name,
        done=done,
        total=len(ops),
        files=touched_files,
        archived_to=archived_to,
        errors=errors + val_errors,
        warnings=rep.warnings,
    )
    return {
        "ok": not (errors or val_errors),
        "done": done,
        "total": len(ops),
        "rolled_back": False,
        "archived_to": archived_to,
        "errors": errors + val_errors,
        "warnings": rep.warnings,
    }


def record_write_ledger(ok, errors=None):
    """命令行回写的成败记给引擎（只在引擎设置了 RPG_WRITE_LEDGER 时）。"""
    ledger = os.environ.get(WRITE_LEDGER_ENV, "").strip()
    if not ledger:
        return
    try:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ok": bool(ok), "errors": [str(e) for e in (errors or [])][:5]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_write_ledger(path):
    """读回 record_write_ledger 写下的记录，按时间顺序返回 [(ok, errors), …]。"""
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append((bool(item.get("ok")), list(item.get("errors") or [])))
    return out


def main():
    ap = argparse.ArgumentParser(description="原子化回写存档状态变化并归档本轮故事")
    ap.add_argument("--save", help="指定存档名")
    ap.add_argument("--file", help="从 JSON 文件读取操作列表")
    ap.add_argument("--json", help="直接传入 JSON 字符串")
    ap.add_argument("--no-rollback", action="store_true", help="校验失败时不回滚已写入文件")
    ap.add_argument("--no-commit", action="store_true", help="不自动创建回合 Git 快照")
    ap.add_argument("--archive-input", default="", help="单独归档模式：玩家输入")
    ap.add_argument("--archive-story", default="", help="单独归档模式：故事正文")
    args = ap.parse_args()

    sd = save_dir(args.save)

    if args.archive_story:
        rel = archive_story_turn(sd, args.archive_input, args.archive_story)
        if not args.no_commit:
            git_turn_checkpoint(sd, args.archive_input)
        print(f"故事原文已归档至 log/story/{rel}")
        return 0

    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.json:
        raw = args.json.strip()
    else:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace").strip()

    if not raw:
        sys.exit("未提供任何 JSON 操作输入")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        record_write_ledger(False, [f"JSON 格式错误：{e}"])
        sys.exit(f"JSON 格式错误：{e}")

    res = apply_operations(
        sd,
        payload,
        rollback_on_error=not args.no_rollback,
        commit_checkpoint=not args.no_commit,
    )
    record_write_ledger(not res["rolled_back"], res["errors"])

    if res["rolled_back"]:
        print(f"回写失败（已原子回滚全部 {res['total']} 处更改，存档保持干净）：")
        for e in res["errors"]:
            print("  " + e)
        return 1

    msg = f"已回写 {res['done']}/{res['total']} 处"
    if res["ok"]:
        msg += "，校验通过"
    if res.get("archived_to"):
        msg += f"，正文已归档（{res['archived_to']}）"
    print(msg)
    for e in res["errors"]:
        print("  " + e)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
