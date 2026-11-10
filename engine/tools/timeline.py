"""存档时间线：回合快照的回滚、撤回与重演底座。

  python engine/tools/timeline.py undo            撤回最近一轮（保留回合外的场外修改）
  python engine/tools/timeline.py log -n 15       列出最近的快照
  python engine/tools/timeline.py --save <存档> undo

提交前缀的含义：
  [turn]                   一个正式回合（每回合恰好一个）
  [tuner] [gm] [sync]      回合外的场外修改：撤回回合时保留
  [polish] [save] [snapshot] 依附于被撤回回合的内容：随回合一起撤销
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_error, log_event  # noqa: E402
from apply import git_turn_checkpoint  # noqa: E402
from common import GIT_IDENTITY, read_text, save_dir  # noqa: E402

REPLAY_PREFIXES = ("[tuner]", "[gm]", "[sync]")
# 与时间线无关的文件：不进快照，回滚时也不受影响
UNTRACKED_FILES = ("log/tuner_history.json", "log/pending_turn.json", "world/seed_status.yaml", "world/candidate.yaml")


def has_git(sd):
    return bool(shutil.which("git")) and (Path(sd) / ".git").is_dir()


def _git(sd, *args, check=True):
    return subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(sd), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def head(sd):
    if not has_git(sd):
        return ""
    return _git(sd, "rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()


def ensure_ignored(sd):
    """让调教窗聊天记录这类文件脱离快照（兼容旧存档），否则撤回回合会连带清空它们。"""
    if not has_git(sd):
        return
    path = Path(sd) / ".gitignore"
    existing = read_text(path) if path.exists() else ""
    have = {line.strip() for line in existing.splitlines()}
    missing = [name for name in UNTRACKED_FILES if name not in have]
    if not missing:
        return
    prefix = existing.rstrip() + "\n" if existing.strip() else ""
    path.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
    for name in missing:
        _git(sd, "rm", "--cached", "-q", "--ignore-unmatch", name, check=False)


def commit_pending(sd, label="回合外改动"):
    """把工作区里尚未提交的改动存成 [sync] 快照，保证回滚只会丢弃回合自身产生的内容。"""
    if not has_git(sd):
        return False
    ensure_ignored(sd)
    return git_turn_checkpoint(sd, label, prefix="[sync]")


def restore(sd, sha):
    """把存档精确恢复到某个快照：已跟踪文件还原，回合中新建的未跟踪文件一并清除。"""
    if not has_git(sd) or not sha:
        return False
    try:
        _git(sd, "reset", "--hard", sha)
        _git(sd, "clean", "-fdq")
        return True
    except Exception as exc:
        log_error("timeline.restore_err", exc=exc, save=Path(sd).name, sha=sha)
        return False


def fold_turn(sd, pre_head, label):
    """把一个回合内产生的全部改动（含外部 Agent 自行提交的）折叠成唯一一个 [turn] 快照。"""
    if not has_git(sd):
        return False
    if pre_head and head(sd) != pre_head:
        _git(sd, "reset", "--soft", pre_head)
    return git_turn_checkpoint(sd, label, prefix="[turn]")


def story_changed_since(sd, pre_head):
    """判断回合期间是否已有人写入故事归档；无法判断（没有 git）时返回 None。"""
    if not has_git(sd):
        return None
    if _git(sd, "status", "--porcelain", "--", "log/story", check=False).stdout.strip():
        return True
    if pre_head and head(sd) != pre_head:
        return bool(_git(sd, "diff", "--name-only", pre_head, "HEAD", "--", "log/story", check=False).stdout.strip())
    return False


def locate_last_turn(sd):
    """返回 (最近一个 [turn] 提交, 其后所有提交 [(sha, 标题)]，由旧到新)；找不到时返回 (None, [])。"""
    if not has_git(sd):
        return None, []
    later = []
    for line in _git(sd, "log", "--format=%H%x09%s", check=False).stdout.splitlines():
        sha, _, subject = line.partition("\t")
        subject = subject.strip()
        if subject.startswith("[turn]"):
            if not _git(sd, "rev-parse", "--verify", "-q", f"{sha}~1", check=False).stdout.strip():
                return None, []
            later.reverse()
            return sha, later
        later.append((sha, subject))
    return None, []


def _replay(sd, commits):
    """把回合之后的场外修改重新应用到回滚后的时间线；与被撤回内容冲突的不强行合并。"""
    kept, dropped = [], []
    for sha, subject in commits:
        if not subject.startswith(REPLAY_PREFIXES):
            continue
        result = _git(sd, "cherry-pick", sha, check=False)
        if result.returncode == 0:
            kept.append(subject)
            continue
        conflicted = _git(sd, "diff", "--name-only", "--diff-filter=U", check=False).stdout.strip()
        _git(sd, "cherry-pick", "--abort", check=False)
        if conflicted:
            dropped.append(subject)
    return kept, dropped


def _strip_last_story_block(sd):
    """没有可用快照时的兜底：只从故事归档里摘掉最后一轮。"""
    story_dir = Path(sd) / "log" / "story"
    files = sorted(story_dir.glob("*/*.md")) if story_dir.is_dir() else []
    for path in reversed(files):
        raw = read_text(path)
        if not raw.strip():
            continue
        parts = re.split(r"(?m)^---[ \t]*$", raw)
        if len(parts) > 2:
            path.write_text("---".join(parts[:-1]).rstrip() + "\n\n", encoding="utf-8")
        else:
            path.unlink()
        return True
    return False


def undo_last_turn(sd):
    """撤回最近一轮。pre_head 可用于在重演失败时把存档原样恢复。"""
    commit_pending(sd)
    pre_head = head(sd)
    turn_sha, later = locate_last_turn(sd)
    kept, dropped = [], []
    via_git = False
    if turn_sha and restore(sd, f"{turn_sha}~1"):
        via_git = True
        kept, dropped = _replay(sd, later)
    else:
        _strip_last_story_block(sd)
    log_event("timeline.undo", save=Path(sd).name, via_git=via_git, kept=kept, dropped=dropped)
    return {"pre_head": pre_head, "via_git": via_git, "kept": kept, "dropped": dropped}


def describe_undo(result):
    """把撤回结果翻译成给玩家看的一句话；没有需要说明的情况时返回空串。"""
    parts = []
    if result.get("kept"):
        parts.append(f"已保留 {len(result['kept'])} 条场外修改")
    if result.get("dropped"):
        names = "、".join(s.split("：", 1)[-1] for s in result["dropped"][:3])
        parts.append(f"{len(result['dropped'])} 条与被撤回内容冲突、未能保留（{names}）")
    return "；".join(parts)


def main():
    ap = argparse.ArgumentParser(description="存档时间线工具")
    ap.add_argument("--save", default=None, help="存档名（默认当前存档）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("undo", help="撤回最近一轮")
    log_p = sub.add_parser("log", help="列出最近的快照")
    log_p.add_argument("-n", type=int, default=15)
    args = ap.parse_args()
    sd = save_dir(args.save)
    if args.cmd == "log":
        print(_git(sd, "log", f"-{args.n}", "--format=%h %s", check=False).stdout.rstrip())
        return
    result = undo_last_turn(sd)
    print("已撤回最近一轮" + (f"（{describe_undo(result)}）" if describe_undo(result) else ""))


if __name__ == "__main__":
    main()
