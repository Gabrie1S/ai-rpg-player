"""游戏时钟。事件触发由这里判断，不靠 AI 记性。

  python engine/tools/clock.py now               当前时间、地点、在场人物
  python engine/tools/clock.py advance 7         推进 7 天，列出到期和进入前兆期的事件
  python engine/tools/clock.py advance 30 --force   冻结状态下也强制推进（/gm）
  python engine/tools/clock.py date 120          第 120 天是哪天
  python engine/tools/clock.py upcoming 90       未来 90 天的计划事件（上帝视角，有剧透）
  python engine/tools/clock.py mark-ticked       幕后推演完成后调用，记下推演到哪天
  python engine/tools/clock.py mark-snapshot     世界快照生成后调用
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_event  # noqa: E402
from common import day_to_date, entity_name, load_world, load_yaml, read_frontmatter, save_dir, write_frontmatter  # noqa: E402

ACTIVE = ("planned", "rumored", "changed")
# 引擎安排的幕后推演会设置这个环境变量：进度由引擎验收后记录，命令行里的 mark-ticked 不再生效
ENGINE_SETTLES_TICK_ENV = "RPG_ENGINE_SETTLES_TICK"


def load_now(sd):
    path = sd / "world" / "now.md"
    meta, body = read_frontmatter(path)
    if meta is None:
        sys.exit("world/now.md 缺少 YAML 头")
    return path, meta, body


def load_events(sd):
    data = load_yaml(sd / "world" / "events" / "timeline.yaml") or {}
    return [e for e in (data.get("events") or []) if isinstance(e, dict)]


def fmt_event(sd, cal, e):
    day = e.get("day")
    loc = entity_name(sd, e.get("location")) if e.get("location") else "地点未定"
    return f"  - [{e.get('id')}] 第{day}天（{day_to_date(day, cal)}）{e.get('title', '')} @ {loc}  状态：{e.get('status')}"


def cmd_now(sd):
    _, meta, _ = load_now(sd)
    world = load_world(sd)
    cal = world.get("calendar")
    day = int(meta.get("day", 0))
    print(f"第{day}天，{day_to_date(day, cal)}")
    print(f"地点：{entity_name(sd, meta.get('location'))}")
    present = meta.get("present") or []
    print("在场：" + ("、".join(entity_name(sd, p) for p in present) if present else "无"))
    since = day - int(meta.get("last_tick_day", 0))
    print(f"距上次推演：{since} 天（常规间隔 {world.get('tick_days', 7)} 天）")
    snap = day - int(meta.get("last_snapshot_day", 0))
    print(f"距上次世界快照：{snap} 天（常规间隔 {world.get('snapshot_days', 90)} 天）")
    if meta.get("frozen"):
        print("⚠ 世界已冻结（/gm），推演暂停")


def cmd_advance(sd, n, force, reason):
    if n <= 0:
        sys.exit("天数必须大于 0")
    path, meta, body = load_now(sd)
    if meta.get("frozen") and not force:
        sys.exit("世界已冻结，不能推进时间。用 /gm 解冻，或加 --force。")
    world = load_world(sd)
    cal = world.get("calendar")
    old = int(meta.get("day", 0))
    new = old + n
    meta["day"] = new
    write_frontmatter(path, meta, body)

    events = load_events(sd)
    due = [e for e in events if e.get("status") in ACTIVE and old < int(e.get("day", -1)) <= new]
    omen = [e for e in events if e.get("status") == "planned" and e not in due
            and old < int(e.get("day", 0)) - int(e.get("foreshadow_days", 0) or 0) <= new]
    overdue = [e for e in events if e.get("status") in ACTIVE and int(e.get("day", 10**9)) <= old]
    missed_omen = [e for e in events if e.get("status") == "planned" and e not in due and e not in omen
                   and int(e.get("day", 0)) - int(e.get("foreshadow_days", 0) or 0) <= old < int(e.get("day", 0))]

    print(f"时间推进 {n} 天：第{old}天 → 第{new}天（{day_to_date(new, cal)}）" + (f"  ｜{reason}" if reason else ""))
    if due:
        print("\n到期事件（需要结算）：")
        for e in sorted(due, key=lambda x: x.get("day", 0)):
            print(fmt_event(sd, cal, e))
    if omen:
        print("\n进入前兆期（开始放风声）：")
        for e in sorted(omen, key=lambda x: x.get("day", 0)):
            print(fmt_event(sd, cal, e))
            if e.get("rumor"):
                print(f"      风声：{e['rumor']}")
    if overdue:
        print("\n⚠ 早已到期却未结算的事件：")
        for e in overdue:
            print(fmt_event(sd, cal, e))
    if missed_omen:
        print("\n⚠ 已在前兆期但状态仍是 planned：")
        for e in missed_omen:
            print(fmt_event(sd, cal, e))

    since = new - int(meta.get("last_tick_day", 0))
    tick_days = int(world.get("tick_days", 7))
    if due or omen or overdue or missed_omen or since >= tick_days:
        print(f"\n→ 建议调用 world-sim 推演：第{int(meta.get('last_tick_day', 0)) + 1}天 ~ 第{new}天"
              f"（距上次推演 {since} 天）")
    else:
        print("\n无需推演。")
    snap = new - int(meta.get("last_snapshot_day", 0))
    if snap >= int(world.get("snapshot_days", 90)):
        print(f"→ 建议生成世界快照（距上次 {snap} 天）")
    log_event(
        "clock.advance",
        save=sd.name,
        days=n,
        old_day=old,
        new_day=new,
        reason=reason,
        due_ids=[e.get("id") for e in due],
        omen_ids=[e.get("id") for e in omen],
        needs_sim=bool(due or omen or overdue or missed_omen or since >= tick_days),
    )


def cmd_upcoming(sd, n):
    _, meta, _ = load_now(sd)
    cal = load_world(sd).get("calendar")
    day = int(meta.get("day", 0))
    events = [e for e in load_events(sd) if e.get("status") in ACTIVE and day <= int(e.get("day", -1)) <= day + n]
    print(f"第{day}天 ~ 第{day + n}天的计划事件：")
    if not events:
        print("  （无）")
    for e in sorted(events, key=lambda x: x.get("day", 0)):
        print(fmt_event(sd, cal, e))
        fs = int(e.get("foreshadow_days", 0) or 0)
        if fs:
            print(f"      前兆期从第{int(e['day']) - fs}天开始")


def cmd_mark_ticked(sd):
    if os.environ.get(ENGINE_SETTLES_TICK_ENV) == "1":
        # 引擎安排的推演由引擎在验收通过后记录进度；模型先记上，写回却失败时，到期事件就会被跳过
        print("这次推演由引擎在验收通过后自动记录进度，不需要 mark-ticked。改完存档、校验通过后直接写简报即可。")
        return
    path, meta, body = load_now(sd)
    meta["last_tick_day"] = int(meta.get("day", 0))
    write_frontmatter(path, meta, body)
    print(f"已记录：推演到第{meta['last_tick_day']}天")


def cmd_mark_snapshot(sd):
    path, meta, body = load_now(sd)
    meta["last_snapshot_day"] = int(meta.get("day", 0))
    write_frontmatter(path, meta, body)
    print(f"已记录：世界快照于第{meta['last_snapshot_day']}天")


def main():
    p = argparse.ArgumentParser(description="游戏时钟")
    p.add_argument("--save", help="存档名，默认读 saves/current.txt")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now")
    a = sub.add_parser("advance")
    a.add_argument("days", type=int)
    a.add_argument("--force", action="store_true")
    a.add_argument("--reason", default="")
    d = sub.add_parser("date")
    d.add_argument("day", type=int)
    u = sub.add_parser("upcoming")
    u.add_argument("days", type=int, nargs="?", default=90)
    sub.add_parser("mark-ticked")
    sub.add_parser("mark-snapshot")
    args = p.parse_args()
    sd = save_dir(args.save)

    if args.cmd == "now":
        cmd_now(sd)
    elif args.cmd == "advance":
        cmd_advance(sd, args.days, args.force, args.reason)
    elif args.cmd == "date":
        print(f"第{args.day}天：{day_to_date(args.day, load_world(sd).get('calendar'))}")
    elif args.cmd == "upcoming":
        cmd_upcoming(sd, args.days)
    elif args.cmd == "mark-ticked":
        cmd_mark_ticked(sd)
    elif args.cmd == "mark-snapshot":
        cmd_mark_snapshot(sd)


if __name__ == "__main__":
    main()
