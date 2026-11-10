"""幕后推演掷骰工具。

  python engine/tools/dice.py 2d6+3            任意表达式，可多个
  python engine/tools/dice.py check 2 8        判定：2d6 + 修正 2，对比难度 8
  python engine/tools/dice.py check -1 困难 --note "黑石帮突袭"

结果会追加到当前存档的 log/dice.log。
"""
import argparse
import datetime
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_log import log_event  # noqa: E402
from common import load_yaml, read_frontmatter, save_dir  # noqa: E402

RNG = random.SystemRandom()
TERM = re.compile(r"([+-]?)(\d*)d(\d+)|([+-]?)(\d+)")
DEFAULT_DC = {"简单": 6, "普通": 8, "困难": 10, "极难": 12, "传说": 14}


def roll_expr(expr):
    expr = expr.replace(" ", "").lower()
    pos, total, parts = 0, 0, []
    while pos < len(expr):
        m = TERM.match(expr, pos)
        if not m or m.end() == pos:
            sys.exit(f"看不懂的骰子表达式：{expr}")
        if m.group(3):
            sign = -1 if m.group(1) == "-" else 1
            n, faces = int(m.group(2) or 1), int(m.group(3))
            if n > 100 or faces < 2:
                sys.exit("骰子数量或面数不合理")
            rolls = [RNG.randint(1, faces) for _ in range(n)]
            total += sign * sum(rolls)
            parts.append(f"{'-' if sign < 0 else ''}{n}d{faces}{rolls}")
        else:
            sign = -1 if m.group(4) == "-" else 1
            total += sign * int(m.group(5))
            parts.append(f"{'-' if sign < 0 else '+'}{m.group(5)}")
        pos = m.end()
    return total, " ".join(parts)


def log(line, save_name=None):
    sd = save_dir(save_name, required=False)
    if not sd:
        return
    day = "?"
    try:
        meta, _ = read_frontmatter(sd / "world" / "now.md")
        day = meta.get("day", "?") if meta else "?"
    except Exception:
        pass
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_dir = sd / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "dice.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp}  第{day}天  {line}\n")


def difficulty_table(save_name=None):
    sd = save_dir(save_name, required=False)
    if sd:
        rules = load_yaml(sd / "world" / "rules.yaml") or {}
        if rules.get("difficulty"):
            return rules["difficulty"]
    return DEFAULT_DC


def check(mod, dc_arg, note, save_name=None):
    table = difficulty_table(save_name)
    if dc_arg in table:
        dc, dc_name = int(table[dc_arg]), dc_arg
    else:
        try:
            dc = int(dc_arg)
        except ValueError:
            sys.exit(f"难度要么是数字，要么是：{'、'.join(table)}")
        dc_name = next((k for k, v in table.items() if int(v) == dc), "")
    a, b = RNG.randint(1, 6), RNG.randint(1, 6)
    natural = a + b
    total = natural + mod
    if natural == 12 or total >= dc + 3:
        result = "大成功"
    elif natural == 2:
        result = "大失败"
    elif total >= dc:
        result = "成功"
    elif total >= dc - 2:
        result = "代价成功"
    else:
        result = "失败"
    dc_text = f"{dc}（{dc_name}）" if dc_name else str(dc)
    line = f"判定：2d6[{a}, {b}] = {natural}，修正 {mod:+d}，总值 {total} vs 难度 {dc_text} → {result}"
    if note:
        line += f"  ｜{note}"
    print(line)
    log(line, save_name=save_name)
    log_event(
        "dice.check",
        save=save_name or "",
        rolls=[a, b],
        natural=natural,
        mod=mod,
        total=total,
        dc=dc,
        dc_name=dc_name,
        outcome=result,
        note=note,
    )


def main():
    argv = list(sys.argv[1:])
    save_opt = None
    cleaned = []
    i = 0
    while i < len(argv):
        if argv[i] == "--save" and i + 1 < len(argv):
            save_opt = argv[i + 1]
            i += 2
        elif argv[i].startswith("--save="):
            save_opt = argv[i].split("=", 1)[1]
            i += 1
        else:
            cleaned.append(argv[i])
            i += 1

    if cleaned and cleaned[0] == "check":
        p = argparse.ArgumentParser(prog="dice.py check")
        p.add_argument("cmd")
        p.add_argument("mod", type=int, help="修正值，可为负")
        p.add_argument("dc", help="难度数值或名称")
        p.add_argument("--note", default="", help="这次判定是为了什么")
        a = p.parse_args(cleaned)
        check(a.mod, a.dc, a.note, save_name=save_opt)
        return
    p = argparse.ArgumentParser(description="掷骰")
    p.add_argument("expr", nargs="+", help="如 2d6+3、1d100")
    p.add_argument("--note", default="")
    a = p.parse_args(cleaned)
    for e in a.expr:
        total, detail = roll_expr(e)
        line = f"{e}：{detail} = {total}" + (f"  ｜{a.note}" if a.note else "")
        print(line)
        log(line, save_name=save_opt)


if __name__ == "__main__":
    main()
