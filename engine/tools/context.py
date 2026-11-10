"""智能上下文预装配器（Context Assembler）、双通道括号反馈提取与深度置底防遗忘锚点（Author's Note）。

核心机制：
1. 在玩家发出行动后、调用大模型之前，由 Python 秒级扫描：
   - world/now.md（当前时间、所在地点卡、在场 NPC 卡、进行中的事/待处理）
   - world/stage.md（本阶段必读清单 + 「什么时候 → 读什么」触发词匹配）
   - 玩家本轮输入中提及的任何已知 NPC / 地点 / 势力名称
   - 玩家本轮输入中顺带夹写的括号场外微调指令（双通道反馈 · 通道 A）
   自动将这些卡片内容与当轮微调要求打包进上下文，省去 AI 第一步调 Read 工具的网络往返。
2. 在上下文最末尾生成紧贴最新输入的「本轮导演与通用叙事提示词锚点（Author's Note）」：
   - 采用单一通用叙事提示词模板（不再按模型硬编码紧箍咒字典），支持玩家随时自由修改；
   - 若外部世界包文件夹被意外删除，自动使用存档内 `world/lore.md` 与 `world/rules.yaml` 兜底组装系统提示词，确保游玩不中断。

用法：
  python engine/tools/context.py --input "我向老霍克打听冷却液的事（场外：注意他现在还不信任我）"
  python engine/tools/context.py --json --input "我向老霍克打听冷却液的事"
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    DEFAULT_NARRATIVE_PROMPT,
    ROOT,
    SKILLS,
    active_pack_id,
    day_to_date,
    entity_name,
    load_config,
    load_theme,
    load_ui_schema,
    load_world,
    mods_dir,
    pack_dir,
    read_frontmatter,
    read_text,
    safe_load_yaml,
    save_dir,
)
from clock import ACTIVE, load_events  # noqa: E402
import chapters as chapter_log  # noqa: E402
import plot  # noqa: E402
import story_log  # noqa: E402
from turn_protocol import (  # noqa: E402
    MISSING_CHANGES_NOTE,
    PROTOCOL_API,
    PROTOCOL_BACK_API,
    PROTOCOL_GENESIS_API,
    PROTOCOL_INLINE,
    PROTOCOL_TOOLS,
    PROTOCOL_TUNER_API,
)

# 匹配玩家在主输入框中顺手写的显式场外/导演括号指令（通道 A）
INLINE_OOC_RE = re.compile(
    r"[（(【\[]\s*(?:OOC|ooc|场外|导演|微调|吐槽|提示|注意|要求|补充|备注)\s*[：:]?\s*([^）)】\]]+?)\s*[）)】\]]"
)


def extract_inline_director_notes(player_input):
    """提取玩家行动文本中夹带的顺手场外导演指令，返回 (clean_input, notes_list)。"""
    text = (player_input or "").strip()
    if not text:
        return "", []
    notes = [m.strip() for m in INLINE_OOC_RE.findall(text) if m.strip()]
    if not notes:
        return text, []
    cleaned = INLINE_OOC_RE.sub("", text).strip()
    return (cleaned or "（继续推进当前场景）"), notes


def collect_entity_lookup(sd):
    """返回 [(实体名, 相对路径, ID)] 供玩家输入关键词触发自动注入。"""
    items = []
    for folder in ("npcs", "locations", "factions"):
        d = sd / "world" / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                meta, _ = read_frontmatter(p)
                if meta and meta.get("name"):
                    rel = f"world/{folder}/{p.name}"
                    items.append((str(meta["name"]), rel, str(meta.get("id", p.stem))))
            except Exception:
                continue
    return items


def parse_stage_triggers(stage_text, player_input):
    """解析 stage.md 中的必读文件与表格触发项，返回命中的相对存档路径列表。"""
    files = []
    if not stage_text:
        return files
    if "## 台前必读" in stage_text:
        sec = stage_text.split("## 台前必读", 1)[1].split("\n## ", 1)[0]
        for m in re.findall(r"`([^`]+\.md)`", sec):
            rel = m if m.startswith(("world/", "player/", "log/")) else f"world/{m}"
            if rel not in files:
                files.append(rel)
    if "## 用来埋伏笔的秘密" in stage_text:
        sec = stage_text.split("## 用来埋伏笔的秘密", 1)[1].split("\n## ", 1)[0]
        for m in re.findall(r"`([^`]+\.md)`", sec):
            rel = m if m.startswith(("world/", "player/", "log/")) else f"world/{m}"
            if rel not in files:
                files.append(rel)
    if "## 用到时再读" in stage_text and player_input:
        sec = stage_text.split("## 用到时再读", 1)[1].split("\n## ", 1)[0]
        for line in sec.splitlines():
            if "|" not in line or "---" in line:
                continue
            parts = [c.strip() for c in line.strip("|").split("|")]
            if len(parts) >= 2:
                cond, targets = parts[0], parts[1]
                keywords = [k.strip() for k in re.split(r"[、，,/（）()\s]+", cond) if len(k.strip()) >= 2]
                if any(k in player_input for k in keywords):
                    for m in re.findall(r"`([^`]+\.md)`", targets):
                        rel = m if m.startswith(("world/", "player/", "log/")) else f"world/{m}"
                        if rel not in files:
                            files.append(rel)
    return files


def build_authors_note(sd, cfg=None, inline_notes=None):
    """构建 Depth-1 置底防遗忘锚点（Author's Note），融合当轮括号导演微调指令与通用可编辑提示词模板。"""
    cfg = cfg or load_config()
    try:
        now_meta, _ = read_frontmatter(sd / "world" / "now.md")
    except Exception:
        now_meta = {}
    now_meta = now_meta or {}
    world = load_world(sd)
    cal = world.get("calendar")
    day = int(now_meta.get("day", 0))
    date_str = day_to_date(day, cal)
    loc_str = entity_name(sd, now_meta.get("location"))
    present = [entity_name(sd, p) for p in (now_meta.get("present") or [])]

    lines = [
        "【本轮提示】",
        f"- 当前时空：第 {day + 1} 天（{date_str}）｜地点：{loc_str}｜在场：{'、'.join(present) if present else '无'}",
    ]

    if inline_notes:
        joined_notes = "；".join(inline_notes)
        lines.append(
            f"- 玩家这轮的要求（照办，体现在描写里，不写 OOC 解释）：{joined_notes}"
        )

    # 难关胜率天平检测
    recent = list(now_meta.get("recent_challenges") or [])
    thresh = int(((cfg.get("context") or {}).get("challenge_streak_threshold")) or 3)
    win_set = {"大成功", "成功", "代价成功"}
    if len(recent) >= thresh and all(r in win_set for r in recent[-thresh:]):
        lines.append(
            f"- 玩家已连续 {thresh} 次通过难关（{recent[-thresh:]}），本轮若有难关从严裁决。"
        )

    upcoming = _upcoming_events(sd, day, cal)
    if upcoming:
        lines.append(f"- 近期固定事件（时间推进不能越过，到了要演出来）：{upcoming}")

    moods = load_theme(sd).get("mood_presets")
    if isinstance(moods, dict) and moods:
        lines.append(f"- 界面气氛：当前 `ui_mood={now_meta.get('ui_mood') or 'default'}`，可选 {' / '.join(moods)}。气氛明显变化时回写 `world/now.md` 的 `ui_mood`。")

    journal_path = sd / "player" / "journal.md"
    messy = journal_overflow(read_text(journal_path)) if journal_path.exists() else []
    if messy:
        lines.append(
            f"- 日志卡该整理了：{'、'.join(messy)}。本轮变更里顺手合并、删减到每栏不超过 {JOURNAL_SECTION_LIMIT} 条、每条一句话，"
            "认识的人每人只留一条，不是秘密的挪出“知道的秘密”。"
        )

    failed = read_pending_turn(sd)
    if failed.get("missing_changes"):
        lines.append("- " + MISSING_CHANGES_NOTE)
    if failed.get("errors"):
        lines.append(
            "- 上一轮这些变更没写进存档，本轮一并修正后重新提交，不要跟玩家提："
            f"原因 {'；'.join(failed['errors'])[:400]}；原操作 {json.dumps(failed.get('ops') or [], ensure_ascii=False)[:1500]}"
        )

    # 通用叙事提示词模板（允许玩家在 config.yaml 或 Web UI 中自由修改）
    prompt_cfg = cfg.get("narrative_prompt") or {}
    custom_tpl = (prompt_cfg.get("authors_note_template") or "").strip() or DEFAULT_NARRATIVE_PROMPT
    mod_note = mods_dir(sd) / "authors_note.md" if sd else None
    if mod_note and mod_note.is_file():
        custom_tpl = read_text(mod_note).strip() or custom_tpl
    lines.append("- " + custom_tpl)
    return "\n".join(lines)


def pending_turn_path(sd):
    return Path(sd) / "log" / "pending_turn.json"


def read_pending_turn(sd):
    """上一轮留下的待办：没写进去的变更、要幕后做的事。"""
    path = pending_turn_path(sd)
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _upcoming_events(sd, day, cal, window=30, limit=5):
    try:
        events = [e for e in load_events(sd) if e.get("status") in ACTIVE and day < int(e.get("day", -1)) <= day + window]
    except Exception:
        return ""
    events.sort(key=lambda e: int(e.get("day", 0)))
    return "；".join(f"{day_to_date(int(e['day']), cal)} {e.get('title', e.get('id', ''))}" for e in events[:limit])


def _cap_sections(text, limit, only=None, keep_first=False):
    """小节太长时只放最近几条（索引放最前面几条）。完整内容一直在文件里，模型用到时可以读。"""
    parts = re.split(r"(?m)^(?=## )", text)
    out = [parts[0]]
    for part in parts[1:]:
        title, _, rest = part.partition("\n")
        items = [line for line in rest.split("\n") if line.strip()]
        if (only is None or title[3:].strip() in only) and len(items) > limit:
            kept = items[:limit] if keep_first else items[-limit:]
            part = f"{title}\n（另有 {len(items) - limit} 条在文件里，用到时再读）\n" + "\n".join(kept) + "\n\n"
        out.append(part)
    return "".join(out).strip()


MAX_CONTEXT_FILE_CHARS = 8000
MEMORY_SECTIONS = ("认知", "与玩家的记忆")
JOURNAL_SECTION_LIMIT = 8   # 每栏最多几条，超了提醒主持人合并删减
JOURNAL_LINE_LIMIT = 40     # 每条建议的字数
JOURNAL_CONTEXT_LINE_CHARS = 100


def journal_overflow(text):
    """日志卡里该整理的栏：条数超了，或者有写得太长的条目。"""
    problems = []
    for part in re.split(r"(?m)^(?=## )", text or "")[1:]:
        title, _, rest = part.partition("\n")
        items = [line.strip() for line in rest.split("\n") if line.strip().startswith("-")]
        long_items = [i for i in items if len(i) > JOURNAL_LINE_LIMIT + 20]
        if len(items) > JOURNAL_SECTION_LIMIT or long_items:
            problems.append(f"{title[3:].strip()}（{len(items)} 条" + (f"，{len(long_items)} 条太长" if long_items else "") + "）")
    return problems


def _fit_for_context(rel, text):
    """同一份资料放进每轮上下文时的体积上限。这些文件会随游玩不断变长，不限制的话每轮成本一直涨。"""
    if rel == "world/index.md":
        text = _cap_sections(text, 30, keep_first=True)
    elif rel == "player/journal.md":
        text = _cap_sections(text, 12)
        text = "\n".join(line if len(line) <= JOURNAL_CONTEXT_LINE_CHARS else line[:JOURNAL_CONTEXT_LINE_CHARS] + "…" for line in text.split("\n"))
    elif rel == "world/director.md":
        text = _cap_sections(text, 8)
    elif rel.startswith("world/npcs/"):
        text = _cap_sections(text, 10, only=MEMORY_SECTIONS)
    if len(text) > MAX_CONTEXT_FILE_CHARS:
        text = text[:MAX_CONTEXT_FILE_CHARS].rstrip() + f"\n（后面还有，用到时读 `{rel}`）"
    return text


def _triggered_optional_skills(sd, player_input):
    """世界包的可选能力规则平时不占上下文，玩家用到时才放进这一轮。"""
    pdir = pack_dir(sd=sd, required=False)
    if not pdir or not player_input:
        return []
    meta, _ = safe_load_yaml(pdir / "pack.yaml") if (pdir / "pack.yaml").exists() else ({}, None)
    skills = (meta or {}).get("optional_skills") or []
    if not skills:
        return []
    words = []
    for name, _desc, rel in list_pack_commands(pdir, sd):
        words.append(f"/{name}")
        command_meta, _ = read_frontmatter(ROOT / rel)
        words.extend(str(w) for w in (command_meta or {}).get("triggers") or [])
    return list(skills) if any(w and w in player_input for w in words) else []


def assemble_turn_context(sd, player_input="", cfg=None):
    """自动收集当前回合所需的核心卡片、三级切片目录索引、双通道括号指令与置底锚点，返回结构化字典。"""
    cfg = cfg or load_config()
    try:
        now_meta, _ = read_frontmatter(sd / "world" / "now.md")
    except Exception:
        now_meta = {}
    now_meta = now_meta or {}

    clean_action, inline_notes = extract_inline_director_notes(player_input)

    core_files = [
        "world/now.md",
        "world/stage.md",
        "world/index.md",
        "world/design/目录.md",
        "world/secrets/目录.md",
        "world/director.md",
        "world/canon.md",
        "player/sheet.md",
        "player/journal.md",
        "player/persona.md",
    ]
    if now_meta.get("location"):
        core_files.append(f"world/locations/{now_meta['location']}.md")
    for pid in now_meta.get("present") or []:
        core_files.append(f"world/npcs/{pid}.md")

    stage_path = sd / "world" / "stage.md"
    stage_text = read_text(stage_path) if stage_path.exists() else ""
    for rel in parse_stage_triggers(stage_text, player_input):
        if rel not in core_files:
            core_files.append(rel)

    if player_input:
        for ename, rel, eid in collect_entity_lookup(sd):
            if (ename in player_input or eid in player_input) and rel not in core_files:
                core_files.append(rel)

    loaded = {}
    for rel in core_files:
        p = sd / rel
        if p.exists() and p.is_file():
            loaded[rel] = _fit_for_context(rel, read_text(p).strip())

    current_plot = plot.brief(sd)
    if current_plot:
        loaded["world/design/剧情规划（当前章）"] = current_plot

    previously = chapter_log.context_block(sd, story_log.count_turns(sd))
    if previously:
        loaded["log/summaries.md（前情）"] = previously

    ms_path = sd / "log" / "milestones.md"
    if ms_path.exists():
        mtext = read_text(ms_path).strip()
        if mtext:
            loaded["log/milestones.md"] = mtext[-2000:]

    for skill in _triggered_optional_skills(sd, player_input):
        sk_path = SKILLS / str(skill) / "SKILL.md"
        if sk_path.exists():
            loaded[f"skills/{skill}/SKILL.md"] = read_text(sk_path).strip()

    authors_note = build_authors_note(sd, cfg, inline_notes=inline_notes)
    return {
        "save": sd.name,
        "pack": active_pack_id(sd),
        "day": int(now_meta.get("day", 0)),
        "clean_action": clean_action,
        "inline_director_notes": inline_notes,
        "files": loaded,
        "authors_note": authors_note,
    }


def format_context_markdown(ctx):
    """将预装配的上下文格式化为可直接注入 Prompt 的 Markdown 块。"""
    blocks = ["## 本轮资料（已给出的不要重读）\n"]
    for rel, content in ctx["files"].items():
        blocks.append(f"### `{rel}`\n{content}\n")
    blocks.append("---\n" + ctx["authors_note"])
    return "\n".join(blocks)


def assemble_backstage_context(sd):
    """为幕后推演 / 补剧情规划预装配核心存档状态，省去 API 模式下逐个调用 read_save_file 的多轮往返。"""
    if not sd or not Path(sd).is_dir():
        return ""
    sd = Path(sd)
    core_rels = [
        "world/now.md",
        "world/events/timeline.yaml",
        "world/director.md",
        "world/hooks.md",
        "world/stage.md",
        "world/index.md",
        "world/design/目录.md",
        "world/secrets/目录.md",
    ]
    pf = plot.plot_file(sd)
    if pf and pf.is_file():
        rel_plot = pf.relative_to(sd).as_posix()
        if rel_plot not in core_rels:
            core_rels.append(rel_plot)

    blocks = ["## 当前存档核心资料（已预装，无需重复读取；需要某张具体人物/地点/秘密切片时再按目录读取）\n"]
    for rel in core_rels:
        p = sd / rel
        if p.is_file():
            body = read_text(p).strip()
            if body:
                blocks.append(f"### `{rel}`\n{_fit_for_context(rel, body)}\n")

    recent = story_log.all_turns(sd)[-2:]
    if recent:
        turn_lines = []
        for t in recent:
            hdr = t.get("date_header") or f"第{t.get('index', '?')}轮"
            inp = (t.get("input") or "").strip()
            st = (t.get("story") or "").strip()
            if len(st) > 1500:
                st = st[:700].rstrip() + "\n……\n" + st[-700:].lstrip()
            turn_lines.append(f"#### #{t.get('index', '?')} {hdr}\n玩家：{inp}\n\n{st}")
        blocks.append("### 最近两轮台前经过\n" + "\n\n".join(turn_lines) + "\n")

    return "\n".join(blocks) if len(blocks) > 1 else ""


MAX_INLINE_PACK_DOC_CHARS = 12000


def _ensure_pack_doc_sliced(pdir: Path, dp: Path) -> str:
    """若世界包设定文件超大（> 12KB），自动用 split_md 切片缓存，仅在 System Prompt 中注入总览与目录索引以防吃满上下文。"""
    raw = read_text(dp)
    if len(raw) <= MAX_INLINE_PACK_DOC_CHARS:
        return raw
    slice_dir = pdir / ".slices" / dp.stem
    stamp_file = slice_dir / ".mtime"
    mtime_str = str(dp.stat().st_mtime_ns)
    try:
        from split_md import split as split_markdown_file
        if not (slice_dir / "目录.md").exists() or not stamp_file.exists() or read_text(stamp_file).strip() != mtime_str:
            if slice_dir.is_dir():
                for old_f in slice_dir.glob("*.md"):
                    old_f.unlink()
            split_markdown_file(dp, slice_dir)
            stamp_file.write_text(mtime_str, encoding="utf-8")
        overview = read_text(slice_dir / "00-总览.md").strip() if (slice_dir / "00-总览.md").exists() else raw[:2400]
        catalog = read_text(slice_dir / "目录.md").strip()
        return (
            f"{overview}\n\n---\n"
            f"> 💡 **分层切片索引提示**：原文件 `{dp.name}` 体量较大（{len(raw)} 字），已自动按章节切分。"
            f"下方为完整章节目录，需要某章细节时调用 `read_save_file(rel_path=\"pack_slice:{dp.stem}/<文件名>\")`（或直接读取 `packs/{pdir.name}/.slices/{dp.stem}/<文件名>`）按需精读：\n\n"
            f"{catalog}"
        )
    except Exception:
        return raw


def list_pack_commands(pdir, sd=None):
    """世界包和存档 mods 自带的斜杠命令：[(命令名, 说明, 相对路径)]。同名时存档优先。"""
    found = {}
    if pdir and (Path(pdir) / "commands").is_dir():
        for f in sorted((Path(pdir) / "commands").glob("*.md")):
            meta, _ = read_frontmatter(f)
            found[f.stem] = (str((meta or {}).get("description") or ""), f"packs/{Path(pdir).name}/commands/{f.name}")
    mods = mods_dir(sd) if sd else None
    if mods and (mods / "commands").is_dir():
        for f in sorted((mods / "commands").glob("*.md")):
            meta, _ = read_frontmatter(f)
            found[f.stem] = (str((meta or {}).get("description") or ""), f"saves/{Path(sd).name}/mods/commands/{f.name}")
    return [(name, desc, path) for name, (desc, path) in sorted(found.items())]


def _append_mod_docs(parts, sd, docs):
    """存档定制文档追加在世界包文档之后，冲突时以它为准。"""
    mods = mods_dir(sd) if sd else None
    if not mods or not mods.is_dir():
        return
    for doc in docs:
        path = mods / doc
        if path.is_file():
            parts.append(f"\n\n---\n# 本存档的玩家定制：{doc}（与世界包冲突时以这里为准）\n\n" + read_text(path))


def _append_custom_features(parts, pdir, sd):
    """告诉台前这一局有哪些自定义命令和面板。"""
    commands = list_pack_commands(pdir, sd)
    if commands:
        lines = "\n".join(f"- `/{name}`：{desc}（详细规则在 `{path}`）" for name, desc, path in commands)
        parts.append(f"\n\n---\n# 自定义命令\n\n玩家用这些命令，或用自然语言说要发动对应能力时，按命令文件的规则执行：\n{lines}")
    if not sd:
        return
    panels = [p for p in load_ui_schema(sd).get("panels") or [] if p.get("gm_note")]
    if panels:
        lines = "\n".join(f"- 面板 `{p['id']}`（{p.get('label', '')}）：{p['gm_note']}" for p in panels)
        parts.append(
            "\n\n---\n# 需要你维护数据的自定义面板\n\n"
            "玩家能在界面上看到这些面板。数据有变化时，用回写操作 `{\"op\": \"set_panel_data\", \"panel\": \"面板id\", \"data\": {...}}` 更新。"
            "数据玩家全部可见，绝不能写入主角不知道的内容：\n" + lines
        )


FRONT_DOC_MARK = "<!-- 台前常驻到这里 -->"


def _front_pack_doc(pdir, dp):
    """世界包文档可以用标记分成两半：台前每轮带上前半，后半只列目录，用到时再读。"""
    raw = read_text(dp)
    if FRONT_DOC_MARK not in raw:
        return _ensure_pack_doc_sliced(pdir, dp)
    head, tail = raw.split(FRONT_DOC_MARK, 1)
    chapters = "、".join(re.findall(r"(?m)^## (.+)$", tail))
    return f"{head.rstrip()}\n\n（其余章节用到时再读 `packs/{pdir.name}/{dp.name}`：{chapters}）"


def _append_pack_or_save_fallback(parts, pdir, sd, front=False):
    """优先附加世界包文档（超长文档自动启用分层切片目录索引）；若世界包文件夹被外部删除，平滑回退至存档自带的 lore.md 与 rules.yaml。"""
    if pdir and pdir.is_dir():
        for doc in ("setting.md", "rules.md", "protagonist.md", "style_pack.md"):
            dp = pdir / doc
            if dp.exists():
                body = _front_pack_doc(pdir, dp) if front else _ensure_pack_doc_sliced(pdir, dp)
                parts.append(f"\n\n---\n# 当前题材包设定：`{pdir.name}/{doc}`\n\n" + body)
    elif sd and sd.is_dir():
        for rel in ("world/lore.md", "world/rules.yaml", "world/style_notes.md"):
            fp = sd / rel
            if fp.exists():
                parts.append(f"\n\n---\n# 存档内置设定兜底（原世界包文件夹未挂载）：`{rel}`\n\n" + read_text(fp))


def build_system_prompt(sd=None, role="front", pack_id=None, protocol="tools"):
    """动态组装不同角色（front / back / tuner / ui / genesis）的完整 System Prompt。

    这部分在同一局里基本不变，放在提示词最前面才能命中缓存。protocol 是交付方式：台前用 inline / api / tools，幕后走 API 时传 api。
    """
    pdir = pack_dir(pack_id, sd=sd, required=False)
    pack_meta = {}
    if pdir and (pdir / "pack.yaml").exists():
        pack_meta, _ = safe_load_yaml(pdir / "pack.yaml")
    optional_skills = (pack_meta or {}).get("optional_skills") or []

    parts = []
    if role == "front":
        for sk in ("narrative-craft", "gm-workflow"):
            sk_path = SKILLS / sk / "SKILL.md"
            if sk_path.exists():
                parts.append(read_text(sk_path))
        _append_pack_or_save_fallback(parts, pdir, sd, front=True)
        _append_mod_docs(parts, sd, ("setting.md", "rules.md", "protagonist.md", "style_pack.md"))
        _append_custom_features(parts, pdir, sd)
        if optional_skills:
            names = "、".join(f"`skills/{s}/SKILL.md`" for s in optional_skills)
            parts.append(f"\n\n可选能力的规则：{names}。玩家发动时会随本轮给出，没给时用到再读。")
        parts.append("\n\n---\n" + {"inline": PROTOCOL_INLINE, "api": PROTOCOL_API}.get(protocol, PROTOCOL_TOOLS))

    elif role == "tuner":
        tuner_agent = ROOT / "engine" / "agents" / "tuner-agent.md"
        if not tuner_agent.exists():
            tuner_agent = ROOT / ".claude" / "agents" / "tuner-agent.md"
        if tuner_agent.exists():
            parts.append(read_text(tuner_agent))
        for sk in ("live-tuner", "mod-builder", "ui-designer", "narrative-craft"):
            sk_path = SKILLS / sk / "SKILL.md"
            if sk_path.exists():
                parts.append(read_text(sk_path))
        if pdir and pdir.is_dir():
            for doc in ("pack.yaml", "theme.json", "style_pack.md", "rules.yaml"):
                dp = pdir / doc
                if dp.exists():
                    parts.append(f"\n\n---\n# 当前世界包文件：`{pdir.name}/{doc}`\n\n" + read_text(dp))
        _append_mod_docs(parts, sd, ("pack.yaml", "theme.json", "style_pack.md"))
        if protocol == "api":
            parts.append("\n\n---\n" + PROTOCOL_TUNER_API)

    elif role == "ui":
        # 定制界面只需要界面规范，不需要写作和回合规则
        tuner_agent = ROOT / "engine" / "agents" / "tuner-agent.md"
        if not tuner_agent.exists():
            tuner_agent = ROOT / ".claude" / "agents" / "tuner-agent.md"
        if tuner_agent.exists():
            parts.append(read_text(tuner_agent))
        sk_path = SKILLS / "ui-designer" / "SKILL.md"
        if sk_path.exists():
            parts.append(read_text(sk_path))
        if pdir and pdir.is_dir():
            for doc in ("pack.yaml", "theme.json"):
                dp = pdir / doc
                if dp.exists():
                    parts.append(f"\n\n---\n# 当前世界包文件：`{pdir.name}/{doc}`\n\n" + read_text(dp))
        _append_mod_docs(parts, sd, ("pack.yaml", "theme.json"))
        if protocol == "api":
            parts.append("\n\n---\n" + PROTOCOL_TUNER_API)

    elif role == "genesis":
        # 设计世界包：世界包的文风手册是这里写的，所以写作标准也要看到
        for sk in ("pack-designer", "worldbuilder", "narrative-craft"):
            sk_path = SKILLS / sk / "SKILL.md"
            if sk_path.exists():
                parts.append(read_text(sk_path))
        schema_doc = ROOT / "engine" / "docs" / "schema.md"
        if schema_doc.exists():
            parts.append(read_text(schema_doc))
        if pdir and pdir.is_dir():
            for doc in ("pack.yaml", "setting.md", "rules.md", "rules.yaml", "protagonist.md", "style_pack.md", "backstage.md", "theme.json"):
                dp = pdir / doc
                if dp.exists():
                    parts.append(f"\n\n---\n# 当前世界包文件：`{pdir.name}/{doc}`\n\n" + read_text(dp))
        if protocol == "api":
            parts.append("\n\n---\n" + PROTOCOL_GENESIS_API)

    else:
        sim_agent = ROOT / "engine" / "agents" / "world-sim.md"
        if not sim_agent.exists():
            sim_agent = ROOT / ".claude" / "agents" / "world-sim.md"
        if sim_agent.exists():
            parts.append(read_text(sim_agent))
        # 人物卡的台词、序章剧本、导演笔记都是幕后写的，台前照着演，所以写作标准两边都要有
        craft = SKILLS / "narrative-craft" / "SKILL.md"
        if craft.exists():
            parts.append(read_text(craft))
        schema_doc = ROOT / "engine" / "docs" / "schema.md"
        if schema_doc.exists():
            parts.append(read_text(schema_doc))
        _append_pack_or_save_fallback(parts, pdir, sd)
        backstage = pdir / "backstage.md" if pdir else None
        if backstage and backstage.exists():
            parts.append(f"\n\n---\n# 当前世界包的幕后规则（优先于通用规则）\n\n" + read_text(backstage))
        _append_mod_docs(parts, sd, ("setting.md", "rules.md", "protagonist.md", "style_pack.md", "backstage.md"))
        if protocol == "api":
            parts.append("\n\n---\n" + PROTOCOL_BACK_API)

    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="智能上下文预装配器")
    ap.add_argument("--save", help="存档名")
    ap.add_argument("--input", default="", help="玩家本轮输入（用于触发关键词关联卡片与提取场外括号指令）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    ap.add_argument("--note-only", action="store_true", help="仅输出置底 Author's Note")
    args = ap.parse_args()

    sd = save_dir(args.save)
    if args.note_only:
        _, notes = extract_inline_director_notes(args.input)
        print(build_authors_note(sd, inline_notes=notes))
        return 0
    ctx = assemble_turn_context(sd, args.input)
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(format_context_markdown(ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
