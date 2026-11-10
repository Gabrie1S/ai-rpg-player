"""通用规则表驱动（Schema-Driven）存档数据校验器。

依据 engine/docs/schema.md 与当前存档的 world/rules.yaml（含 schema_mapping）动态校验：
- 角色卡（玩家与 NPC）的阶位、属性区间、先天适性（含隐藏觉醒层）、潜能、唯一主引擎、技能与装备
- 实体 ID 唯一性与双向引用完整性（NPC、地点双向连通、势力时钟、当前状态 now.md、时间线 timeline.yaml）
- 单文件体积限制（防止单个设定文件过大撑爆上下文）

用法：
  python engine/tools/validate.py              校验当前存档，有错误时退出码 1
  python engine/tools/validate.py --save <名>  校验指定存档
  python engine/tools/validate.py --hook       给 CLI Hook 用：从 stdin 读工具调用，有错时输出 stderr 并退出码 2
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    _root = Path(__file__).resolve().parents[2]
    _venv_py = _root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if _venv_py.is_file() and Path(sys.executable).resolve() != _venv_py.resolve():
        sys.exit(subprocess.call([str(_venv_py), *sys.argv]))
    if "--hook" in sys.argv:
        sys.exit(0)
    raise

sys.path.insert(0, str(Path(__file__).parent))
from common import SAVES, load_yaml, read_frontmatter, save_dir, schema_map  # noqa: E402

TIERS = {"A", "B", "C"}
EVENT_STATUS = {"planned", "rumored", "cancelled", "changed", "happened"}
FOLDERS = {"npcs": "npc_", "locations": "loc_", "factions": "fac_"}


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")


def get_ranks(rules):
    return rules.get("ranks") or []


def parse_rank(rank_val, rules, rank_label="rank"):
    """返回 (大阶位定义 dict, 错误信息)。写法：大阶位名 + 等级后缀；levels: false 的基础阶位不带后缀。"""
    if not isinstance(rank_val, str) or not rank_val:
        return None, f"{rank_label} 为空"
    levels = [str(x) for x in (rules.get("levels") or [])]
    ranks = get_ranks(rules)
    for r in sorted(ranks, key=lambda x: -len(str(x["name"]))):
        rname = str(r["name"])
        if rank_val.startswith(rname):
            lv = rank_val[len(rname):]
            if not r.get("levels"):
                return r, (f"阶位「{rname}」不分小级，却写了后缀「{lv}」" if lv else None)
            if levels and lv not in levels:
                return r, f"阶位等级后缀「{lv or '（空）'}」应为 {levels} 之一，如「{rname}{levels[0]}」"
            return r, None
    valid_names = [str(r.get("name")) for r in ranks]
    return None, f"未知阶位「{rank_val}」（合法大阶位：{valid_names}）"


def all_elements(rules):
    e = rules.get("elements") or {}
    if isinstance(e, list):
        return [str(x) for x in e]
    return [str(x) for x in (e.get("basic") or [])] + [str(x) for x in (e.get("variant") or [])]


def check_grade(rep, where, what, grade, rules):
    if grade is None:
        return
    g_str = str(grade)
    allowed_grades = rules.get("allowed_grades")
    if allowed_grades:
        if g_str not in allowed_grades:
            rep.err(where, f"{what}品阶「{g_str}」应为 {allowed_grades} 之一")
        return
    grades = [str(x) for x in (rules.get("grades") or [])]
    steps = [str(x) for x in (rules.get("grade_steps") or [])]
    if not grades:
        return
    if steps:
        valid = (
            {f"{g}{s}" for g in grades for s in steps}
            | {f"{g}{s}品" for g in grades for s in steps}
            | {f"{g}阶{s}品" for g in grades for s in steps}
        )
        if g_str not in valid and g_str not in grades:
            rep.err(where, f"{what}品阶「{g_str}」不合法（可用阶：{grades}，品：{steps}，如「{grades[0]}{steps[0]}品」）")
    elif g_str not in grades:
        rep.err(where, f"{what}品阶「{g_str}」应为 {grades} 之一")


def check_character(rep, where, meta, rules):
    smap = schema_map(rules, meta)
    rank_field = smap["rank_field"]
    aff_field = smap["affinity_field"]
    apt_field = smap["aptitude_field"]
    eng_field = smap["engine_field"]

    rank_def, problem = parse_rank(meta.get(rank_field), rules, rank_field)
    if problem:
        rep.err(where, problem)
    elems = all_elements(rules)

    # 基础属性
    attrs = meta.get("attributes") or {}
    allowed_attrs = rules.get("attributes") or []
    if not isinstance(attrs, dict):
        rep.err(where, "attributes 必须是字典")
        attrs = {}
    for k, v in attrs.items():
        if allowed_attrs and k not in allowed_attrs:
            rep.err(where, f"未知属性「{k}」，应为 {allowed_attrs}")
        if not isinstance(v, (int, float)):
            rep.err(where, f"属性「{k}」不是数字")
        elif rank_def and not meta.get("attr_override"):
            lo, hi = rank_def.get("attr_range", [None, None])
            if lo is not None and not (lo <= v <= hi):
                rep.err(where, f"属性「{k}」={v} 超出 {rank_def['name']} 范围 [{lo}, {hi}]（特例可加 attr_override: true）")

    # 先天适性 / 相性 (支持 _active 与 _shown 隐藏觉醒机制)
    purities = rules.get("affinity_grades") or []

    def check_affinity_dict(field):
        d = meta.get(field)
        if d is None:
            return None
        if not isinstance(d, dict):
            rep.err(where, f"{field} 应为 属性 → 等级 的字典；无适性写 {{}}")
            return {}
        for k, v in d.items():
            if elems and k not in elems:
                rep.err(where, f"{field} 属性「{k}」不在 {elems} 中")
            if purities and v not in purities:
                rep.err(where, f"{field}「{k}」的等级「{v}」应为 {purities}")
        return d

    aff = check_affinity_dict(aff_field) or {}
    active = check_affinity_dict(f"{aff_field}_active")
    check_affinity_dict(f"{aff_field}_shown")
    for k, v in (active or {}).items():
        if k not in aff:
            rep.err(where, f"{aff_field}_active「{k}」不在真实适性 {aff_field} 中")
        elif v in purities and aff[k] in purities and purities.index(v) > purities.index(aff[k]):
            rep.err(where, f"{aff_field}_active「{k}」等级「{v}」高于真实等级「{aff[k]}」")

    # 潜能
    axes = rules.get("aptitude_axes") or []
    grades = rules.get("aptitude_grades") or []
    if axes:
        for field, required in ((apt_field, True), (f"{apt_field}_shown", False)):
            zz = meta.get(field)
            if zz is None and not required:
                continue
            if not isinstance(zz, dict):
                rep.err(where, f"{field} 应为包含 {axes} 的字典")
                continue
            for a in axes:
                if a not in zz:
                    rep.err(where, f"{field} 缺少「{a}」")
            for k, v in zz.items():
                if k not in axes:
                    rep.err(where, f"{field} 未知项「{k}」")
                elif grades and v not in grades:
                    rep.err(where, f"{field}.{k}「{v}」应为 {grades}")

    # 唯一主引擎 (os_core / core_engine)
    eng = meta.get(eng_field)
    if eng is not None:
        if not isinstance(eng, dict) or not eng.get("name"):
            rep.err(where, f"{eng_field} 应为 {{name, element, grade, orientation, limit}} 或 null")
        else:
            el = eng.get("element")
            if elems and el not in elems + ["无", "通用"]:
                rep.err(where, f"主引擎「{eng['name']}」属性「{el}」应为 {elems} 之一或「无/通用」")
            elif el not in ("无", "通用", None) and el not in (active if active is not None else aff):
                rep.err(where, f"主引擎「{eng['name']}」需求属性「{el}」，但当前已生效的 {aff_field} 中不包含该属性")
            check_grade(rep, where, f"主引擎「{eng['name']}」", eng.get("grade"), rules)
            ors = rules.get("orientations") or []
            if ors and eng.get("orientation") not in ors:
                rep.err(where, f"主引擎倾向「{eng.get('orientation')}」应为 {ors}")
            rnames = [str(r["name"]) for r in get_ranks(rules)]
            lim = eng.get("limit")
            if lim is not None and rnames and lim not in rnames:
                rep.err(where, f"主引擎上限「{lim}」应为合法大阶位名 {rnames}")
            elif lim and rank_def and rnames and rnames.index(str(rank_def["name"])) > rnames.index(str(lim)):
                rep.err(where, f"当前阶位 {meta.get(rank_field)} 超过了主引擎「{eng['name']}」的支撑上限 {lim}")

    # 技能
    sk_levels = rules.get("skill_levels") or []
    sk_types = rules.get("skill_types") or []
    for sk in meta.get("skills") or []:
        if not isinstance(sk, dict) or "name" not in sk:
            rep.err(where, f"skills 条目格式应为 {{name, level, element, type, grade}}：{sk}")
            continue
        if sk_levels and sk.get("level") not in sk_levels:
            rep.err(where, f"技能「{sk['name']}」熟练度「{sk.get('level')}」不在 {sk_levels} 中")
        if "element" in sk and elems:
            els = sk["element"] if isinstance(sk["element"], list) else [sk["element"]]
            for e in els:
                if e not in elems + ["无", "通用"]:
                    rep.err(where, f"技能「{sk['name']}」属性「{e}」不在 {elems} 或「无/通用」中")
        if "type" in sk and sk_types and sk["type"] not in sk_types:
            rep.err(where, f"技能「{sk['name']}」类型「{sk['type']}」应为 {sk_types}")
        check_grade(rep, where, f"技能「{sk['name']}」", sk.get("grade"), rules)

    # 装备 / 义体 / 奇物
    tiers = rules.get("gear_tiers") or []
    for g in meta.get("gear") or []:
        if not isinstance(g, dict) or "name" not in g:
            rep.err(where, f"gear 条目格式应为 {{name, tier, bound}}：{g}")
        elif tiers and g.get("tier") not in tiers:
            rep.err(where, f"装备「{g['name']}」品级「{g.get('tier')}」应为 {tiers}")

    # 特殊能力
    for ab in meta.get("abilities") or []:
        if not isinstance(ab, dict) or not ab.get("name"):
            rep.err(where, f"abilities 条目格式应为 {{name, level, notes}}：{ab}")
        elif not isinstance(ab.get("level", 1), int):
            rep.err(where, f"能力「{ab['name']}」的 level 应为整数")


def collect(sd, rep):
    """读取所有实体，返回 {id: (类型, meta, 路径)}。"""
    ents = {}
    for folder, prefix in FOLDERS.items():
        d = sd / "world" / folder
        if not d.is_dir():
            rep.err(f"world/{folder}", "目录不存在")
            continue
        for p in sorted(d.glob("*.md")):
            where = f"world/{folder}/{p.name}"
            try:
                meta, _ = read_frontmatter(p)
            except yaml.YAMLError as e:
                rep.err(where, f"YAML 头解析失败：{str(e).splitlines()[0]}")
                continue
            if meta is None:
                rep.err(where, "缺少 YAML 头")
                continue
            eid = meta.get("id")
            if eid != p.stem:
                rep.err(where, f"id「{eid}」与文件名「{p.stem}」不一致")
            if not str(eid).startswith(prefix):
                rep.warn(where, f"id 建议以 {prefix} 开头")
            if eid in ents:
                rep.err(where, f"id「{eid}」重复（另见 {ents[eid][2]}）")
            if not meta.get("name"):
                rep.err(where, "缺少 name")
            ents[eid] = (folder, meta, where)
    return ents


def ref_ok(rep, where, field, value, ents, kinds, allow_none=True):
    if value is None:
        if not allow_none:
            rep.err(where, f"{field} 不能为空")
        return
    if value == "player" and "player" in kinds:
        return
    if value not in ents or ents[value][0] not in kinds:
        rep.err(where, f"{field} 引用了不存在的 ID「{value}」")


def validate(sd):
    rep = Report()
    rules = load_yaml(sd / "world" / "rules.yaml") or {}
    if not get_ranks(rules):
        rep.err("world/rules.yaml", "缺少 ranks 阶位定义")
    axes = rules.get("attitude_axes") or []
    ents = collect(sd, rep)

    for eid, (kind, m, where) in ents.items():
        if kind == "npcs":
            char_smap = schema_map(rules, m)
            rf = char_smap["rank_field"]
            for f in ("tier", "alive", rf, "attributes", "toward_player"):
                if f not in m:
                    rep.err(where, f"缺少必填字段 {f}")
            if m.get("tier") not in TIERS:
                rep.err(where, f"tier 应为 A/B/C，当前「{m.get('tier')}」")
            ref_ok(rep, where, "faction", m.get("faction"), ents, {"factions"})
            ref_ok(rep, where, "location", m.get("location"), ents, {"locations"})
            for f in ("met_player", "last_seen"):
                if m.get(f) is not None and not isinstance(m.get(f), int):
                    rep.err(where, f"{f} 应为游戏日（整数）或 null")
            check_character(rep, where, m, rules)
            tp = m.get("toward_player") or {}
            for k, v in (tp.items() if isinstance(tp, dict) else []):
                if axes and k not in axes:
                    rep.err(where, f"toward_player 未知维度「{k}」，应为 {axes}")
                if not isinstance(v, (int, float)) or not -100 <= v <= 100:
                    rep.err(where, f"toward_player.{k}={v} 应为 -100 ~ 100 的数字")
            q = m.get("questline")
            if q:
                if not isinstance(q, dict) or not isinstance(q.get("stages"), list):
                    rep.err(where, "questline 需要 stages 列表")
                else:
                    for s in q["stages"]:
                        if not isinstance(s, dict) or "beat" not in s or "id" not in s:
                            rep.err(where, f"questline 节点需要 id 和 beat：{s}")
            elif m.get("tier") == "A":
                rep.warn(where, "A 级 NPC 还没有 questline")
        elif kind == "locations":
            ref_ok(rep, where, "parent", m.get("parent"), ents, {"locations"})
            ref_ok(rep, where, "faction", m.get("faction"), ents, {"factions"})
            for n in m.get("neighbors") or []:
                if not isinstance(n, dict) or "to" not in n:
                    rep.err(where, f"neighbors 条目格式应为 {{to, days}}：{n}")
                    continue
                ref_ok(rep, where, "neighbors.to", n["to"], ents, {"locations"})
                if not isinstance(n.get("days"), (int, float)) or n["days"] <= 0:
                    rep.err(where, f"到「{n['to']}」的 days 应为正数")
                other = ents.get(n["to"])
                if other and other[0] == "locations":
                    back = [x.get("to") for x in other[1].get("neighbors") or [] if isinstance(x, dict)]
                    if eid not in back:
                        rep.warn(where, f"到「{n['to']}」的路是单向的（对方没有回连）")
        elif kind == "factions":
            ref_ok(rep, where, "headquarters", m.get("headquarters"), ents, {"locations"})
            ref_ok(rep, where, "leader", m.get("leader"), ents, {"npcs"})
            c = m.get("clock")
            if c is not None:
                if not isinstance(c, dict) or not isinstance(c.get("progress"), int) or not isinstance(c.get("max"), int):
                    rep.err(where, "clock 需要整数 progress 和 max")
                elif not 0 <= c["progress"] <= c["max"]:
                    rep.err(where, f"clock 进度 {c['progress']}/{c['max']} 越界")
                elif c["progress"] == c["max"]:
                    rep.warn(where, "势力时钟已满，应结算 on_full 后果")
            for k, v in (m.get("relations") or {}).items():
                ref_ok(rep, where, f"relations.{k}", k, ents, {"factions"})
                if not isinstance(v, (int, float)) or not -100 <= v <= 100:
                    rep.err(where, f"relations.{k}={v} 应为 -100 ~ 100")

    # now.md
    where = "world/now.md"
    try:
        now, _ = read_frontmatter(sd / "world" / "now.md")
    except Exception as e:
        now = None
        rep.err(where, f"读取失败：{e}")
    if now is None:
        rep.err(where, "缺少 YAML 头")
        now = {}
    day = now.get("day")
    if not isinstance(day, int) or day < 0:
        rep.err(where, "day 应为非负整数")
        day = 0
    ref_ok(rep, where, "location", now.get("location"), ents, {"locations"})
    for pid in now.get("present") or []:
        ref_ok(rep, where, "present", pid, ents, {"npcs"})
        if pid in ents and ents[pid][0] == "npcs":
            pm = ents[pid][1]
            if pm.get("alive") is False:
                rep.err(where, f"已死亡的 NPC「{pid}」出现在在场名单里")
            if now.get("location") and pm.get("location") != now.get("location"):
                rep.warn(where, f"在场 NPC「{pid}」的 location 是「{pm.get('location') or '空'}」，与当前地点不一致")

    # timeline.yaml
    where = "world/events/timeline.yaml"
    try:
        tl = load_yaml(sd / "world" / "events" / "timeline.yaml") or {}
    except yaml.YAMLError as e:
        rep.err(where, f"YAML 解析失败：{str(e).splitlines()[0]}")
        tl = {}
    seen = set()
    for e in tl.get("events") or []:
        if not isinstance(e, dict):
            rep.err(where, f"事件格式错误：{e}")
            continue
        w = f"{where} [{e.get('id')}]"
        if not e.get("id") or not str(e["id"]).startswith("evt_"):
            rep.err(w, "id 应以 evt_ 开头")
        if e.get("id") in seen or e.get("id") in ents:
            rep.err(w, "id 重复")
        seen.add(e.get("id"))
        if not isinstance(e.get("day"), int):
            rep.err(w, "day 应为整数")
        if e.get("status") not in EVENT_STATUS:
            rep.err(w, f"status「{e.get('status')}」应为 {sorted(EVENT_STATUS)}")
        if e.get("status") == "happened":
            rep.warn(w, "已发生的事件应移到 chronicle.md")
        if isinstance(e.get("day"), int) and e["day"] < day and e.get("status") in ("planned", "rumored", "changed"):
            rep.warn(w, f"第{e['day']}天已过，事件仍未结算")
        if not isinstance(e.get("foreshadow_days", 0), int):
            rep.err(w, "foreshadow_days 应为整数")
        ref_ok(rep, w, "location", e.get("location"), ents, {"locations"})
        for pid in e.get("participants") or []:
            ref_ok(rep, w, "participants", pid, ents, {"npcs", "factions", "player"})

    # 文件大小：太大的文件会占满台前的上下文
    for f in (sd / "world").rglob("*.md"):
        if f.name in ("index.md", "目录.md"):
            continue
        text = f.read_text(encoding="utf-8")
        size = len(text.encode("utf-8"))
        if "## 重要时刻" in text:
            imp = text.split("## 重要时刻", 1)[1].split("\n## ", 1)[0]
            size -= len(imp.encode("utf-8"))
        if size > 12000:
            rep.warn(str(f.relative_to(sd)).replace("\\", "/"), f"文件 {size // 1000}KB，偏大，应按主题拆分或压缩")

    # player
    where = "player/sheet.md"
    try:
        pm, _ = read_frontmatter(sd / "player" / "sheet.md")
        if pm is None:
            rep.err(where, "缺少 YAML 头")
        else:
            check_character(rep, where, pm, rules)
            for k in (pm.get("reputation") or {}):
                ref_ok(rep, where, f"reputation.{k}", k, ents, {"factions"})
    except FileNotFoundError:
        rep.err(where, "文件不存在")
    except yaml.YAMLError as e:
        rep.err(where, f"YAML 头解析失败：{str(e).splitlines()[0]}")
    return rep, ents


def hook_target():
    """hook 模式：返回被改动文件所在的存档目录；不是存档文件则返回 None。"""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    fp = (data.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return None
    try:
        rel = Path(fp).resolve().relative_to(SAVES.resolve())
    except ValueError:
        return None
    if len(rel.parts) < 2:
        return None
    d = SAVES / rel.parts[0]
    return d if d.is_dir() else None


def main():
    p = argparse.ArgumentParser(description="校验存档")
    p.add_argument("--save")
    p.add_argument("--hook", action="store_true")
    args = p.parse_args()

    if args.hook:
        sd = hook_target()
        if not sd:
            return 0
        try:
            from index import build
            build(sd)
        except Exception:
            pass
        rep, _ = validate(sd)
        if rep.errors:
            print(f"存档「{sd.name}」校验发现 {len(rep.errors)} 个错误，请修正：", file=sys.stderr)
            for e in rep.errors:
                print(f"  ✗ {e}", file=sys.stderr)
            return 2
        return 0

    sd = save_dir(args.save)
    rep, ents = validate(sd)
    counts = {k: sum(1 for v in ents.values() if v[0] == k) for k in FOLDERS}
    locked = sum(1 for v in ents.values() if v[1].get("locked"))
    print(f"存档「{sd.name}」：NPC {counts['npcs']}，地点 {counts['locations']}，势力 {counts['factions']}，已锁定 {locked}")
    for e in rep.errors:
        print(f"  ✗ {e}")
    for w in rep.warnings:
        print(f"  ! {w}")
    if not rep.errors and not rep.warnings:
        print("  ✓ 没有问题")
    else:
        print(f"错误 {len(rep.errors)}，警告 {len(rep.warnings)}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
