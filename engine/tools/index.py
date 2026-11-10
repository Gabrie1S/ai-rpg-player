"""从各实体文件的 YAML 头自动生成世界索引 world/index.md（含幕后信息，仅供主持人检索）。

  python engine/tools/index.py            重建当前存档的索引
  python engine/tools/index.py --save X   指定存档
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import load_yaml, read_frontmatter, save_dir, schema_map  # noqa: E402


def load(folder):
    out = {}
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("*.md")):
        try:
            meta, _ = read_frontmatter(p)
        except Exception:
            continue
        if meta and meta.get("id"):
            out[meta["id"]] = meta
    return out


def build(sd):
    w = sd / "world"
    rules = load_yaml(w / "rules.yaml") or {}
    smap = schema_map(rules)
    rf = smap["rank_field"]

    npcs, locs, facs = load(w / "npcs"), load(w / "locations"), load(w / "factions")
    name = lambda d, i: (d.get(i) or {}).get("name", i) if i else "—"
    lines = ["# 世界索引", "", "自动生成，不要手改。一行一个实体；用到时再读对应文件。", ""]

    met = [m for m in npcs.values() if m.get("met_player") is not None]
    if met:
        lines += ["## 主角见过的人", ""]
        for m in sorted(met, key=lambda m: -(m.get("last_seen") or m.get("met_player") or 0)):
            dead = "" if m.get("alive", True) else " · 已故"
            rank_val = m.get(rf) or m.get("rank") or ""
            lines.append(f"- `{m['id']}` {m.get('name')} · {rank_val} · 在 {name(locs, m.get('location'))}"
                         f" · 初见第{m.get('met_player')}天 · 上次第{m.get('last_seen', m.get('met_player'))}天{dead}"
                         + (f" · {m['brief']}" if m.get("brief") else ""))
        lines.append("")

    lines += ["## 人物", ""]
    for tier in ("A", "B", "C"):
        group = [m for m in npcs.values() if m.get("tier") == tier and m.get("met_player") is None]
        for m in group:
            dead = "" if m.get("alive", True) else " · 已故"
            rank_val = m.get(rf) or m.get("rank") or ""
            lines.append(f"- `{m['id']}` {m.get('name')} · {tier} · {rank_val} · {name(facs, m.get('faction'))} · 在 {name(locs, m.get('location'))}{dead}"
                         + (f" · {m['brief']}" if m.get("brief") else ""))
    lines += ["", "## 地点", ""]
    for m in locs.values():
        flag = "已详细" if m.get("detail") == "full" else "只有轮廓"
        known = "" if m.get("discovered") else " · 主角不知道"
        lines.append(f"- `{m['id']}` {m.get('name')} · 属于 {name(locs, m.get('parent'))} · {flag}{known}"
                     + (f" · {' '.join(map(str, m.get('tags') or []))}" if m.get("tags") else ""))
    lines += ["", "## 势力", ""]
    for m in facs.values():
        lines.append(f"- `{m['id']}` {m.get('name')} · {m.get('type', '')} · 据点 {name(locs, m.get('headquarters'))}")
    (w / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(npcs), len(locs), len(facs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save")
    a = ap.parse_args()
    sd = save_dir(a.save, required=False)
    if not sd:
        return
    n, l, f = build(sd)
    print(f"索引已更新：人物 {n}，地点 {l}，势力 {f}")


if __name__ == "__main__":
    main()
