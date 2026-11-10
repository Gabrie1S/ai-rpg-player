"""流式思考链剥离状态机与无剧透阶段状态指示器映射。

防剧透铁律：
- 模型的思考过程（`<think>...</think>` / `<thinking>...</thinking>`）、工具调用参数、读写的文件路径及幕后推演细节，一律在服务端拦截，仅转换为无剧透阶段状态码推送给前端。
"""
import re

_OPEN_THINK_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
_STATIC_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think(?:ing)?>.*$", re.IGNORECASE | re.DOTALL)


def strip_think_blocks(text: str):
    """静态剥离文本中的所有 `<think>...</think>` 思考块，返回 (clean_text, extracted_thinking)。"""
    if not text:
        return "", ""
    extracted = []
    for m in _STATIC_THINK_RE.finditer(text):
        inner = _OPEN_THINK_RE.sub("", m.group(0))
        inner = _CLOSE_THINK_RE.sub("", inner).strip()
        if inner:
            extracted.append(inner)
    cleaned = _STATIC_THINK_RE.sub("", text)
    m_unclosed = _UNCLOSED_THINK_RE.search(cleaned)
    if m_unclosed:
        inner = _OPEN_THINK_RE.sub("", m_unclosed.group(0)).strip()
        if inner:
            extracted.append(inner)
        cleaned = cleaned[:m_unclosed.start()]
    return cleaned.strip(), "\n\n".join(extracted)


class StreamThinkStripper:
    """跨任意分片边界的流式 `<think>...</think>` 剥离状态机。

    保证思考内容 100% 被拦截在服务端内存供 `ai_log` 落盘，绝不向前端泄露半个标签或思考字符。
    """

    def __init__(self, on_think_start=None):
        self.in_think = False
        self.buf = ""
        self.emitted_any = False
        self.think_parts = []
        self.on_think_start = on_think_start
        self._notified_think = False

    def _notify_think(self):
        if not self._notified_think and self.on_think_start:
            self._notified_think = True
            self.on_think_start()

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.buf += chunk
        out = []

        while self.buf:
            if self.in_think:
                m = _CLOSE_THINK_RE.search(self.buf)
                if m:
                    self.think_parts.append(self.buf[:m.start()])
                    self.buf = self.buf[m.end():].lstrip("\r\n")
                    self.in_think = False
                    continue
                # 保留末尾最多 12 个字符以防 `</thinking>` 跨 chunk 断裂
                lt_idx = self.buf.rfind("<", max(0, len(self.buf) - 12))
                if lt_idx > 0:
                    self.think_parts.append(self.buf[:lt_idx])
                    self.buf = self.buf[lt_idx:]
                elif lt_idx == -1 and len(self.buf) > 12:
                    self.think_parts.append(self.buf[:-12])
                    self.buf = self.buf[-12:]
                break

            m = _OPEN_THINK_RE.search(self.buf)
            if m:
                before = self.buf[:m.start()]
                if before.strip() or (self.emitted_any and before):
                    out.append(before)
                    self.emitted_any = True
                self.buf = self.buf[m.end():]
                self.in_think = True
                self._notify_think()
                continue

            # 若尚未输出任何可见字符且当前仅有空白，暂存等待后续是否紧跟 `<think>`
            if not self.emitted_any and not self.buf.strip():
                break

            # 检查末尾是否存在 `<think>` 或 `<thinking>` 的不完整前缀
            lt_idx = self.buf.rfind("<", max(0, len(self.buf) - 11))
            if lt_idx != -1:
                tail_low = self.buf[lt_idx:].lower()
                if "<think>".startswith(tail_low) or "<thinking>".startswith(tail_low):
                    prefix = self.buf[:lt_idx]
                    if prefix and (self.emitted_any or prefix.strip()):
                        out.append(prefix)
                        self.emitted_any = True
                    self.buf = self.buf[lt_idx:]
                    break

            out.append(self.buf)
            self.emitted_any = True
            self.buf = ""
            break

        return "".join(out)

    def finish(self) -> str:
        if self.in_think:
            if self.buf:
                self.think_parts.append(self.buf)
            self.buf = ""
            self.in_think = False
            return ""
        rem = self.buf
        self.buf = ""
        if not self.emitted_any:
            rem = rem.lstrip("\r\n")
        return rem

    @property
    def thinking_text(self) -> str:
        return "".join(self.think_parts).strip()


def make_spoiler_safe_status(role: str, kind: str, tool_name: str = "") -> dict:
    """将内部执行阶段映射为无剧透前端状态载荷（严禁包含任何路径、参数、实体名或推演内容）。"""
    if role == "back":
        if kind == "thinking":
            return {"type": "status", "phase": "world_sim_think", "label": "🌐 幕后势力与因果正在推演…", "world_sim": True}
        if kind == "tool":
            if tool_name == "roll_dice":
                return {"type": "status", "phase": "world_sim_dice", "label": "🌐 幕后命运之骰正在转动…", "world_sim": True}
            if tool_name in ("apply_changes", "validate_save", "mark_ticked"):
                return {"type": "status", "phase": "world_sim_sync", "label": "🌐 暗流余波正在落定…", "world_sim": True}
        return {"type": "status", "phase": "world_sim", "label": "🌐 幕后世界正在悄然演化…", "world_sim": True}

    if role == "tuner":
        if kind == "thinking":
            return {"type": "status", "phase": "thinking", "label": "正在构思定制方案…", "world_sim": False}
        if kind == "writing":
            return {"type": "status", "phase": "writing", "label": "正在生成调教反馈…", "world_sim": False}
        if kind == "tool":
            tmap = {
                "read_tuner_target": ("reading", "正在检视当前配置…"),
                "update_world_ui": ("ui_apply", "正在应用世界 UI 并运行体检…"),
                "update_narrative_style": ("style_apply", "正在更新文风与提示词配置…"),
                "update_world_or_save": ("world_apply", "正在同步设定并校验合规性…"),
                "write_mods": ("ui_apply", "正在写入本局定制层并运行校验…"),
                "remove_mod": ("state_sync", "正在移除定制项…"),
                "polish_latest_story": ("polish", "正在原地润色正文段落…"),
            }
            ph, lbl = tmap.get(tool_name, ("state_sync", "正在执行场外调教…"))
            return {"type": "status", "phase": ph, "label": lbl, "world_sim": False}
        return {"type": "status", "phase": "thinking", "label": "正在处理场外请求…", "world_sim": False}

    if role == "genesis":
        if kind == "thinking":
            return {"type": "status", "phase": "thinking", "label": "正在构思世界架构与规则体系…", "world_sim": False}
        if kind == "tool":
            if tool_name == "read_pack_raw_files":
                return {"type": "status", "phase": "reading", "label": "正在解析原始设定文稿…", "world_sim": False}
            if tool_name == "write_pack_bundle":
                return {"type": "status", "phase": "pack_write", "label": "正在构建世界包并运行深度体检…", "world_sim": False}
            if tool_name == "update_world_ui":
                return {"type": "status", "phase": "ui_apply", "label": "正在生成定制化世界 UI…", "world_sim": False}
        if kind == "writing":
            return {"type": "status", "phase": "writing", "label": "正在整理创世简报…", "world_sim": False}
        return {"type": "status", "phase": "thinking", "label": "正在执行创世步骤…", "world_sim": False}

    # 默认 role == "front"（台前主持人）
    if kind == "thinking":
        return {"type": "status", "phase": "thinking", "label": "正在构思叙事脉络…", "world_sim": False}
    if kind == "writing":
        return {"type": "status", "phase": "writing", "label": "✍️ 正在书写本轮故事…", "world_sim": False}
    if kind == "tool":
        if tool_name == "call_world_sim":
            return {"type": "status", "phase": "world_sim", "label": "🌐 幕后世界正在悄然演化…", "world_sim": True}
        if tool_name == "clock_advance":
            return {"type": "status", "phase": "clock", "label": "⏳ 时间正在流逝…", "world_sim": False}
        if tool_name == "read_save_file":
            return {"type": "status", "phase": "reading", "label": "正在回溯场景线索…", "world_sim": False}
        if tool_name == "manage_timeline":
            return {"type": "status", "phase": "reverting", "label": "↩️ 正在检视或回溯时间线快照…", "world_sim": False}
        return {"type": "status", "phase": "state_sync", "label": "正在铭刻世界状态…", "world_sim": False}
    return {"type": "status", "phase": "thinking", "label": "正在感知当前场景…", "world_sim": False}
