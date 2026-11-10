"""台前一轮的交付方式：怎么把状态变化交回引擎。

模型写完正文后附一个变更块（命令行和 API 驱动都这样），一次调用完成一轮：玩家马上能看到正文，
也省掉模型调工具回写时的额外往返（每次往返都要重新发送全部上下文，还要再想一遍）。
"""
import json
import re

OPS_DOC = """操作（`file` 都相对存档根目录；`world/now.md` 各栏只写眼下这一幕的处境与待办，旧事不要越攒越长）：
- `{"op":"set","file":"world/now.md","key":"location","value":"loc_x"}` 改 YAML 头字段，`key` 可用点号（`attributes.力量`）
- `{"op":"append","file":"world/npcs/npc_x.md","section":"## 认知","text":"- [第3天][亲眼][真] ……"}` 往小节末尾加一行
- `{"op":"replace","file":"…","old":"原文（必须恰好出现一次）","new":"新文"}`；删掉一整行就把 `old` 写成那一行（带上结尾的换行），`new` 写空
- `{"op":"add_file","file":"world/npcs/npc_y.md","content":"完整文件内容"}` 新建文件
- `{"op":"append_file","file":"world/events/chronicle.md","text":"……"}` 文件末尾追加
- `{"op":"set_panel_data","panel":"面板id","data":{}}` 更新自定义面板
- `{"op":"advance_days","days":7,"reason":"赶路"}` 推进游戏时间"""

OPS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["set", "append", "replace", "add_file", "append_file", "set_panel_data", "advance_days"],
            "description": "操作类型",
        },
        "file": {"type": "string", "description": "相对存档根目录的文件路径，如 world/now.md、player/journal.md"},
        "key": {"type": "string", "description": "op=set 时要修改的 YAML 头字段名，可用点号如 attributes.力量"},
        "value": {"description": "op=set 时要写入的新值"},
        "section": {"type": "string", "description": "op=append 时的小节标题，如 ## 认知"},
        "text": {"type": "string", "description": "op=append 或 append_file 时追加的文本内容"},
        "old": {"type": "string", "description": "op=replace 时文件中原有的文本片段（必须唯一）"},
        "new": {"type": "string", "description": "op=replace 时替换后的新文本（删整行时可写空字符串）"},
        "content": {"type": "string", "description": "op=add_file 时新建文件的完整内容"},
        "days": {"type": "integer", "description": "op=advance_days 时推进的游戏天数（正整数）"},
        "reason": {"type": "string", "description": "op=advance_days 时推进时间的事由"},
        "panel": {"type": "string", "description": "op=set_panel_data 时的自定义面板 ID"},
        "data": {"type": "object", "description": "op=set_panel_data 时写入面板的 JSON 对象"},
    },
    "required": ["op"],
}

PROTOCOL_INLINE = f"""# 这一轮怎么交付

一次写完，不要调用工具回写。先写给玩家看的正文，正文写完后另起一行，输出一个变更块（玩家看不到，引擎会校验后写入）：

<state_changes>
[{{"op":"set","file":"world/now.md","key":"location","value":"loc_x"}}]
</state_changes>

没有变化就写 `[]`。写在块外的所有内容都会给玩家看，所以不要写过程说明。

{OPS_DOC}
- `{{"op":"request_sim","task":"造物","detail":"玩家要去的新地点和需要的内容"}}` 需要幕后结算或造物时写这条；幕后会在下一轮开始前做好。眼下需要的细节你先按常理写，用 `add_file` 建个简卡

资料都在上下文里。确实缺文件才用读文件的工具一次读完（读的时候不写任何文字），不要读 `engine/` 或 `web/` 下的代码。"""

PROTOCOL_API = """# 这一轮怎么交付

本轮所有变化用一次 `apply_changes` 工具提交，校验报错就修正后重交，不向玩家提报错。然后写正文，写完即停。"""

# 幕后走 API 时没有命令行，提示词里的脚本命令换成对应工具
PROTOCOL_BACK_API = f"""# 在这里怎么读写

这里没有命令行。上面提到的命令一律换成工具：读文件用 `read_save_file`，写文件用 `apply_changes`（一次提交一批操作，报错就改好重交），掷骰用 `roll_dice`，改完用 `validate_save` 校验，推演结束用 `mark_ticked`。不知道有哪些文件时，用 `read_save_file` 读目录（比如 `world/design`）就会列出里面的文件；上下文已经给出的文件不要重读。

{OPS_DOC}"""

PROTOCOL_TUNER_API = f"""# 在这里怎么读写

这里没有命令行。上面提到的脚本命令一律换成工具：
- 查存档文件或列出目录（如 `save:world/npcs`、`pack:`）用 `read_tuner_target`；
- 改界面配色、字体、对话框样式、像素画或状态栏用 `update_world_ui`；
- 改通用叙事提示词或文风正反例用 `update_narrative_style`；
- 改当前世界的角色性格（`player/persona.md`）、属性（`player/sheet.md`）、人物卡（`world/npcs/<id>.md`）、剧情走向（`world/director.md`）用 `update_world_or_save` 的 `save_ops`；
- 新增或修改本存档专属面板、命令或 `mods/` 覆盖文件用 `write_mods`。

`save_ops` 支持的{OPS_DOC}"""

PROTOCOL_GENESIS_API = """# 在这里怎么读写

这里没有命令行。上面提到的命令一律换成工具：读取原始散装设定文件用 `read_pack_raw_files`，一次性写入或更新世界包全部文件用 `write_pack_bundle`，微调世界包 UI 与像素边框用 `update_world_ui`。"""

PROTOCOL_TOOLS = f"""# 这一轮怎么交付

本轮所有变化用一条命令提交（不要用 Edit 或 Write 改存档，也不要另开子代理记账）。校验报错会自动回滚，修正后重交，不向玩家提报错。然后写正文，写完即停。不用看脚本源码。

`python engine/tools/apply.py --save <存档名> --json '[…]'`（JSON 较长或引号麻烦时用 `--file ops.json`）

需要唤醒独立幕后 `world-sim` 推演或造物时：Claude Code 可调用 `world-sim` 子代理，其他 CLI / Harness 直接运行 `python engine/tools/world_sim.py --save <存档名> --task "<推演|造物|开局种子生成>" --range "<时间段>" --request "<具体事项>"` 获取无剧透简报。

{OPS_DOC}"""

OPEN_TAG, CLOSE_TAG = "<state_changes>", "</state_changes>"

# 放在每轮输入的最后：系统提示末尾那段说明隔着上万字，有的模型（实测 DeepSeek）会整个忘掉
INLINE_TAIL = (
    "\n\n（写完正文，另起一行附上 <state_changes>[…]</state_changes>："
    "这一轮的时间、地点、在场人物、人物认知、手记有变化就写进去，没有变化也要写 []。）"
)

MISSING_CHANGES_NOTE = (
    "上一轮正文后面漏了 <state_changes> 变更块，存档没有跟着正文更新。"
    "这一轮的变更块里先补上上一轮该改的（过了几天、人在哪、谁在场、人物知道了什么、手记），再写这一轮的。"
)


RECOVER_CHANGES_ASK = (
    "这一轮正文已经给玩家看了，但正文后面漏了变更块。现在只输出这一轮的变更块，不要重复正文，也不要写别的字：\n"
    "<state_changes>\n[…]\n</state_changes>\n"
    "按正文里实际发生的写：过了几天就 advance_days，人在哪、谁在场改 world/now.md，人物的态度、认知和记忆改人物卡，"
    "手记、性格依据、平生大事按规则写。没有变化就写 []。"
)


def has_changes_block(text):
    return OPEN_TAG in (text or "")


def split_changes(text):
    """把模型输出拆成（正文，操作列表，解析错误）。块写坏时正文仍然保留。"""
    raw = text or ""
    start = raw.rfind(OPEN_TAG)
    if start < 0:
        return raw.strip(), [], ""
    story = raw[:start].rstrip()
    body = raw[start + len(OPEN_TAG):]
    end = body.find(CLOSE_TAG)
    body = (body if end < 0 else body[:end]).strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body).strip()
    if not body:
        return story, [], ""
    try:
        ops = json.loads(body)
    except json.JSONDecodeError as exc:
        ops = _salvage_ops(body)
        if ops is None:
            return story, [], f"变更块不是合法的 JSON：{exc}；原文：{_excerpt(body, exc.pos)}"
        try:
            from ai_log import log_event
            log_event("turn.changes_salvaged", lvl="WARN", err=str(exc), ops=len(ops) if isinstance(ops, list) else 1)
        except Exception:
            pass
    if isinstance(ops, dict):
        ops = ops.get("ops") if isinstance(ops.get("ops"), list) else [ops]
    if not isinstance(ops, list) or not all(isinstance(op, dict) for op in ops):
        return story, [], f"变更块必须是操作列表；原文：{body[:600]}"
    return story, ops, ""


# 数组提前收尾后接着写、两段数组首尾相连之后，末尾允许剩下的零碎
_TRAILING_JUNK = set("]}, \t\r\n")


def _salvage_ops(body):
    """模型常见的几种写坏法，能确定原意的才救回来，拿不准就返回 None（交给原有的报错与补救流程）：
    - 数组提前收尾后接着写：`[{…}]},{…}]`、`[{…}],{…}]`
    - 两段数组首尾相连：`[{…}]\\n[{…}]`
    - 末尾多出括号或逗号：`[{…}]]`
    - 对象或数组末尾多一个逗号：`{…,}`、`[{…},]`
    只接受“一串完整的 JSON 值 + 末尾只剩括号逗号空白”；中间有半截或末尾有文字（多半是被截断）都不救。
    """
    for text in (body, re.sub(r",\s*([\]}])", r"\1", body)):
        ops = _decode_json_sequence(text)
        if ops is not None:
            return ops
    return None


def _decode_json_sequence(text):
    decoder = json.JSONDecoder()
    values, pos, size = [], 0, len(text)
    while pos < size:
        # 值与值之间多出来的逗号、提前收尾留下的 `]` `}` 都跳过
        while pos < size and text[pos] in _TRAILING_JUNK:
            pos += 1
        if pos >= size:
            break
        try:
            value, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        values.append(value)
    if not values or not set(text[pos:]) <= _TRAILING_JUNK:
        return None
    ops = []
    for value in values:
        if isinstance(value, list):
            ops.extend(value)
        elif isinstance(value, dict):
            ops.extend(value["ops"] if isinstance(value.get("ops"), list) else [value])
        else:
            return None
    return ops if all(isinstance(op, dict) for op in ops) else None


def _excerpt(body, pos, radius=300):
    """报错时给出出错位置附近的原文，方便下一轮让模型补写或人工排查。"""
    if pos is None or len(body) <= radius * 2:
        return body[: radius * 2]
    start = max(0, pos - radius)
    return ("…" if start else "") + body[start: pos + radius] + "…"


class ChangesStreamFilter:
    """流式输出时拦下变更块，玩家只看到正文。标签可能被拆在两段里，所以末尾疑似标签的部分先留着。"""

    def __init__(self, on_text):
        self.on_text = on_text
        self.buffer = ""
        self.hidden = False

    def feed(self, chunk):
        if self.hidden or not chunk:
            return
        self.buffer += chunk
        at = self.buffer.find(OPEN_TAG)
        if at >= 0:
            self._emit(self.buffer[:at].rstrip())
            self.buffer = ""
            self.hidden = True
            return
        keep = 0
        for size in range(min(len(OPEN_TAG) - 1, len(self.buffer)), 0, -1):
            if OPEN_TAG.startswith(self.buffer[-size:]):
                keep = size
                break
        ready = self.buffer[:-keep] if keep else self.buffer
        # 标签前的空行也先留着，否则正文末尾会多出空行
        stripped = ready.rstrip()
        self.buffer = self.buffer[len(stripped):]
        self._emit(stripped)

    def finish(self):
        if not self.hidden:
            self._emit(self.buffer)
        self.buffer = ""

    def reset(self):
        """模型读完文件后重新开始输出。"""
        self.buffer = ""
        self.hidden = False

    def _emit(self, text):
        if text:
            self.on_text(text)
