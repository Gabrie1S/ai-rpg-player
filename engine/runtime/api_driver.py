"""Direct API 驱动器（支持 DeepSeek / OpenAI / Anthropic / Gemini / Ollama 等协议，原生支持 SSE 流式输出与防剧透思考过滤）。

模块化拆分结构：
- `stream_filter.py`：思考链剥离状态机（`StreamThinkStripper`）与无剧透状态指示器（`make_spoiler_safe_status`）
- `driver_config.py`：供应商预设、鉴权解析、思考强度参数映射与动态超时计算
- `tool_sandbox.py`：分角色工具 Schema（`OPS_ITEM_SCHEMA`）与安全边界执行沙箱（`execute_tool`）
- `api_driver.py`（本文件）：HTTP / SSE 传输、Anthropic 协议转换与多步 Tool Calling 循环（同时重导出上述符号保持向后兼容）
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from ai_log import log_error, log_event  # noqa: E402

from .driver_config import (  # noqa: E402, F401
    API_PROVIDER_PRESETS,
    DEFAULT_BACK_EFFORT,
    DEFAULT_FRONT_EFFORT,
    EFFORT_LEVELS,
    _anthropic_messages_url,
    _credential_endpoint,
    _detect_api_format,
    _has_openai_agents_sdk,
    _is_claude_cli,
    _openai_chat_url,
    _probe_local_http,
    _with_cli_model,
    _with_effort,
    api_effort_fields,
    front_driver_for_turn,
    front_effort,
    has_driver_ready,
    is_api_like_driver,
    is_local_api,
    resolve_api_key,
    resolve_api_timeout,
    resolve_back_driver,
)
from .stream_filter import (  # noqa: E402, F401
    StreamThinkStripper,
    make_spoiler_safe_status,
    strip_think_blocks,
)
from .tool_sandbox import (  # noqa: E402, F401
    ALLOWED_PACK_WRITE_DIRS,
    ALLOWED_PACK_WRITE_FILES,
    ALL_KNOWN_TOOL_NAMES,
    BACK_TOOLS_OPENAI,
    FRONT_FORBIDDEN_PREFIXES,
    FRONT_INLINE_TOOLS_OPENAI,
    FRONT_TOOLS_OPENAI,
    GENESIS_TOOLS_OPENAI,
    TUNER_TOOLS_OPENAI,
    EngineToolFault,
    ToolRunGuard,
    _safe_pack_id,
    _safe_pack_relpath,
    _writes_to_mods,
    execute_tool,
)

# 试过不认思考强度或温度参数的 (接口地址, 模型)，本次运行内不再带
_NO_EFFORT_PARAMS = set()
_NO_TEMP_MODELS = set()


def _omits_temperature(model: str) -> bool:
    """OpenAI o1/o3/o4/gpt-5 及纯推理模型不支持自定义 temperature，默认省略避免 400。"""
    m = (model or "").strip().lower().rsplit("/", 1)[-1]
    if m.startswith(("o1", "o3", "o4", "gpt-5")):
        return True
    return any(tag in m for tag in ("reasoner", "qwq", "-r1"))


def _extract_error_msg(err_obj) -> str:
    if isinstance(err_obj, dict):
        return str(err_obj.get("message") or err_obj.get("detail") or err_obj.get("code") or err_obj)[:400]
    return str(err_obj)[:400]


def _parse_tool_arguments(raw_args):
    """稳健解析模型返回的 tool_calls arguments（容忍外围 Markdown 代码围栏或重复流式 JSON 对象）。"""
    if isinstance(raw_args, dict):
        return raw_args, None
    s = str(raw_args if raw_args is not None else "").strip()
    if not s:
        return {}, None
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.DOTALL).strip()
    if not s:
        return {}, None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed, None
        return None, f"参数必须是 JSON 对象，实际收到 {type(parsed).__name__}"
    except Exception as exc:
        try:
            obj, end_idx = json.JSONDecoder().raw_decode(s.lstrip())
            tail = s.lstrip()[end_idx:].strip()
            if isinstance(obj, dict) and (not tail or tail == s.lstrip()[:end_idx].strip()):
                return obj, None
        except Exception:
            pass
        return None, str(exc)


# ==================== HTTP JSON 与流式 SSE 传输底座 ====================

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib 会把 Authorization / x-api-key 带到重定向目标。
        return None


_api_opener = urllib.request.build_opener(_NoRedirect())


def _post_json(url, headers, payload, timeout=150.0):
    """优先使用 httpx，未安装时自动回退至 Python 标准库 urllib.request（零外部依赖）。"""
    if httpx is not None:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if not 200 <= resp.status_code < 300:
                raise RuntimeError(f"API 请求失败 ({resp.status_code})：{resp.text[:400]}")
            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"API 返回错误：{_extract_error_msg(data['error'])}")
            return data

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _api_opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"API 返回错误：{_extract_error_msg(data['error'])}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else str(e)
        raise RuntimeError(f"API 请求失败 ({e.code})：{body[:400]}")


def _iter_sse_json_packets(url, headers, payload, timeout=180.0):
    """流式 POST 请求并逐帧解析 OpenAI / Anthropic SSE JSON 数据包（同时兼容代理直接返回普通 JSON）。"""
    if httpx is not None:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                if not 200 <= resp.status_code < 300:
                    err_body = resp.read().decode("utf-8", "replace")
                    raise RuntimeError(f"API 流式请求失败 ({resp.status_code})：{err_body[:400]}")
                ctype = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" not in ctype:
                    raw = resp.read().decode("utf-8", "replace")
                    yield {"_non_stream_full": json.loads(raw)}
                    return
                for line in resp.iter_lines():
                    if not line:
                        continue
                    s = line.strip()
                    if not s.startswith("data:"):
                        continue
                    data_str = s[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        yield json.loads(data_str)
                    except Exception:
                        continue
        return

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _api_opener.open(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype:
                raw = resp.read().decode("utf-8", "replace")
                yield {"_non_stream_full": json.loads(raw)}
                return
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    yield json.loads(data_str)
                except Exception:
                    continue
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else str(e)
        raise RuntimeError(f"API 流式请求失败 ({e.code})：{body[:400]}")


def _merge_tool_call_delta(tc_map: dict, tc_delta: dict):
    """合并流式分片中的 tool_calls 增量（兼容标准 OpenAI/DeepSeek 及重复发送 id/name 的第三方网关）。"""
    idx = int(tc_delta.get("index", 0) or 0)
    entry = tc_map.setdefault(
        idx,
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    tid = tc_delta.get("id")
    if tid and not entry["id"]:
        entry["id"] = str(tid)
    fn_obj = tc_delta.get("function") or {}
    fn_name = fn_obj.get("name")
    if fn_name:
        cur_name = entry["function"]["name"]
        if not cur_name:
            entry["function"]["name"] = str(fn_name)
        elif cur_name not in ALL_KNOWN_TOOL_NAMES and fn_name != cur_name:
            entry["function"]["name"] = cur_name + str(fn_name)
    fn_args = fn_obj.get("arguments")
    if fn_args:
        entry["function"]["arguments"] += str(fn_args)


def _execute_step_streaming(url, headers, payload, role, emit_status, on_delta, on_step_reset, prebuffer_chars=48, timeout=180.0):
    """执行单轮流式 LLM 调用：剥离思考链、拦截工具前废话、实时推送正文分片，并返回完整聚合消息。"""
    stream_payload = {**payload, "stream": True}
    api_reasoning_parts = []
    raw_content_parts = []
    tc_map = {}
    usage = None
    finish_reason = ""

    def _on_think():
        emit_status(make_spoiler_safe_status(role, "thinking"))

    stripper = StreamThinkStripper(on_think_start=_on_think)
    pre_buf = []
    pre_buf_len = 0
    flushed_to_ui = False
    saw_tool_call = False

    def _push_visible(txt: str):
        nonlocal pre_buf_len, flushed_to_ui
        if not txt or on_delta is None or saw_tool_call:
            return
        if not flushed_to_ui:
            pre_buf.append(txt)
            pre_buf_len += len(txt)
            if pre_buf_len >= prebuffer_chars:
                emit_status(make_spoiler_safe_status(role, "writing"))
                on_delta("".join(pre_buf))
                pre_buf.clear()
                flushed_to_ui = True
        else:
            on_delta(txt)

    try:
        packets = _iter_sse_json_packets(url, headers, stream_payload, timeout=timeout)
        for pkt in packets:
            if "_non_stream_full" in pkt:
                full_data = pkt["_non_stream_full"]
                if isinstance(full_data, dict) and full_data.get("error"):
                    raise RuntimeError(f"API 返回错误：{_extract_error_msg(full_data['error'])}")
                choice = (full_data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = full_data.get("usage")
                finish_reason = choice.get("finish_reason") or ""
                has_api_r = ("reasoning_content" in msg and msg.get("reasoning_content") is not None) or ("reasoning" in msg and msg.get("reasoning") is not None)
                r_txt = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
                if r_txt:
                    _on_think()
                c_txt = msg.get("content") or ""
                raw_content_parts.append(c_txt)
                tcalls = msg.get("tool_calls") or []
                clean_c, inline_th = strip_think_blocks(c_txt)
                if not tcalls and clean_c and on_delta is not None:
                    emit_status(make_spoiler_safe_status(role, "writing"))
                    on_delta(clean_c)
                    flushed_to_ui = True
                return {
                    "content": clean_c,
                    "raw_content": c_txt,
                    "reasoning": "\n".join(p for p in (r_txt, inline_th) if p),
                    "api_reasoning": r_txt,
                    "has_api_reasoning": has_api_r,
                    "tool_calls": tcalls,
                    "usage": usage,
                    "finish_reason": finish_reason,
                }

            if pkt.get("error"):
                raise RuntimeError(f"API 流式返回错误：{_extract_error_msg(pkt['error'])}")
            if pkt.get("usage"):
                usage = pkt["usage"]
            choices = pkt.get("choices") or []
            if not choices:
                continue
            ch0 = choices[0]
            if ch0.get("finish_reason"):
                finish_reason = ch0["finish_reason"]
            delta = ch0.get("delta") or {}

            r_chunk = delta.get("reasoning_content") if "reasoning_content" in delta else delta.get("reasoning")
            if r_chunk is not None:
                api_reasoning_parts.append(str(r_chunk))
                if r_chunk:
                    _on_think()

            tc_deltas = delta.get("tool_calls")
            if tc_deltas:
                if not saw_tool_call:
                    saw_tool_call = True
                    pre_buf.clear()
                    if flushed_to_ui and on_step_reset:
                        on_step_reset()
                for tcd in tc_deltas:
                    _merge_tool_call_delta(tc_map, tcd)

            c_chunk = delta.get("content")
            if c_chunk:
                raw_content_parts.append(str(c_chunk))
                visible = stripper.feed(str(c_chunk))
                if visible:
                    _push_visible(visible)

    except Exception as stream_exc:
        if not raw_content_parts and not tc_map and "stream" in str(stream_exc).lower() and "流式返回错误" not in str(stream_exc):
            log_event("llm.stream_fallback", lvl="WARN", role=role, reason=str(stream_exc))
            data = _post_json(url, headers, payload, timeout=timeout)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            clean_c, inline_th = strip_think_blocks(msg.get("content") or "")
            has_api_r = ("reasoning_content" in msg and msg.get("reasoning_content") is not None) or ("reasoning" in msg and msg.get("reasoning") is not None)
            r_api = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
            r_txt = "\n".join(x for x in (r_api, inline_th) if x)
            tcalls = msg.get("tool_calls") or []
            if not tcalls and clean_c and on_delta is not None:
                emit_status(make_spoiler_safe_status(role, "writing"))
                on_delta(clean_c)
            return {
                "content": clean_c,
                "raw_content": msg.get("content") or "",
                "reasoning": r_txt,
                "api_reasoning": r_api,
                "has_api_reasoning": has_api_r,
                "tool_calls": tcalls,
                "usage": data.get("usage"),
                "finish_reason": choice.get("finish_reason") or "",
            }
        raise

    if not finish_reason:
        raise RuntimeError("API 流式响应在返回结束状态前中断")
    tail_visible = stripper.finish()
    if tail_visible and not saw_tool_call:
        _push_visible(tail_visible)

    tcalls_list = [tc_map[k] for k in sorted(tc_map.keys())]
    if tcalls_list:
        if flushed_to_ui and on_step_reset:
            on_step_reset()
        pre_buf.clear()
    elif pre_buf and on_delta is not None:
        emit_status(make_spoiler_safe_status(role, "writing"))
        on_delta("".join(pre_buf))
        pre_buf.clear()

    api_r_text = "".join(api_reasoning_parts)
    full_raw_content = "".join(raw_content_parts)
    clean_final, _ = strip_think_blocks(full_raw_content)
    return {
        "content": clean_final,
        "raw_content": full_raw_content,
        "reasoning": "\n".join(p for p in (api_r_text, stripper.thinking_text) if p),
        "api_reasoning": api_r_text,
        "has_api_reasoning": bool(api_reasoning_parts),
        "tool_calls": tcalls_list,
        "usage": usage,
        "finish_reason": finish_reason,
    }


# ==================== Anthropic 原生 /v1/messages 协议适配层 ====================

def _convert_tools_to_anthropic(openai_tools: list) -> list:
    out = []
    for t in openai_tools or []:
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _convert_messages_to_anthropic(chat_msgs: list):
    sys_parts = []
    anth_msgs = []
    for m in chat_msgs:
        r = m.get("role")
        if r == "system":
            if m.get("content"):
                sys_parts.append(str(m["content"]))
            continue
        if r == "assistant":
            blocks = []
            txt = (m.get("content") or "").strip()
            if txt:
                blocks.append({"type": "text", "text": txt})
            for tc in m.get("tool_calls") or []:
                fn_obj = tc.get("function") or {}
                raw_a = fn_obj.get("arguments") or "{}"
                try:
                    inp_obj = json.loads(raw_a) if isinstance(raw_a, str) else (raw_a or {})
                except Exception:
                    inp_obj = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "toolu_1",
                    "name": fn_obj.get("name") or "",
                    "input": inp_obj,
                })
            if blocks:
                if not anth_msgs:
                    anth_msgs.append({"role": "user", "content": "（开启序幕）"})
                if anth_msgs[-1]["role"] == "assistant":
                    prev_c = anth_msgs[-1]["content"]
                    if isinstance(prev_c, list):
                        prev_c.extend(blocks)
                    else:
                        anth_msgs[-1]["content"] = [{"type": "text", "text": str(prev_c)}] + blocks
                else:
                    anth_msgs.append({"role": "assistant", "content": blocks})
        elif r == "tool":
            tr_block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "toolu_1",
                "content": str(m.get("content") or ""),
            }
            if anth_msgs and anth_msgs[-1]["role"] == "user" and isinstance(anth_msgs[-1]["content"], list):
                anth_msgs[-1]["content"].append(tr_block)
            else:
                anth_msgs.append({"role": "user", "content": [tr_block]})
        else:
            u_txt = str(m.get("content") or "（继续）")
            if anth_msgs and anth_msgs[-1]["role"] == "user":
                prev_u = anth_msgs[-1]["content"]
                if isinstance(prev_u, list):
                    prev_u.append({"type": "text", "text": u_txt})
                else:
                    anth_msgs[-1]["content"] = f"{prev_u}\n\n{u_txt}"
            else:
                anth_msgs.append({"role": "user", "content": u_txt})
    if not anth_msgs:
        anth_msgs.append({"role": "user", "content": "（继续）"})
    return "\n\n".join(sys_parts), anth_msgs


def _parse_anthropic_full_response(data: dict):
    reasoning_parts = []
    text_parts = []
    tcalls = []
    for blk in data.get("content") or []:
        btype = blk.get("type")
        if btype == "thinking":
            if blk.get("thinking"):
                reasoning_parts.append(str(blk["thinking"]))
        elif btype == "text":
            text_parts.append(str(blk.get("text") or ""))
        elif btype == "tool_use":
            tcalls.append({
                "id": blk.get("id") or f"toolu_{len(tcalls)+1}",
                "type": "function",
                "function": {
                    "name": blk.get("name") or "",
                    "arguments": json.dumps(blk.get("input") or {}, ensure_ascii=False),
                },
            })
    raw_txt = "".join(text_parts)
    clean_c, inline_th = strip_think_blocks(raw_txt)
    if inline_th:
        reasoning_parts.append(inline_th)
    return {
        "content": clean_c,
        "raw_content": raw_txt,
        "reasoning": "\n".join(p for p in reasoning_parts if p),
        "tool_calls": tcalls,
        "usage": data.get("usage"),
        "finish_reason": data.get("stop_reason") or "",
    }


def _execute_anthropic_step_streaming(url, headers, payload, role, emit_status, on_delta, on_step_reset, prebuffer_chars=48, timeout=180.0):
    stream_payload = {**payload, "stream": True}
    reasoning_parts = []
    raw_content_parts = []
    block_map = {}
    usage = None
    finish_reason = ""

    def _on_think():
        emit_status(make_spoiler_safe_status(role, "thinking"))

    stripper = StreamThinkStripper(on_think_start=_on_think)
    pre_buf = []
    pre_buf_len = 0
    flushed_to_ui = False
    saw_tool_call = False

    def _push_visible(txt: str):
        nonlocal pre_buf_len, flushed_to_ui
        if not txt or on_delta is None or saw_tool_call:
            return
        if not flushed_to_ui:
            pre_buf.append(txt)
            pre_buf_len += len(txt)
            if pre_buf_len >= prebuffer_chars:
                emit_status(make_spoiler_safe_status(role, "writing"))
                on_delta("".join(pre_buf))
                pre_buf.clear()
                flushed_to_ui = True
        else:
            on_delta(txt)

    packets = _iter_sse_json_packets(url, headers, stream_payload, timeout=timeout)
    for pkt in packets:
        if "_non_stream_full" in pkt:
            res = _parse_anthropic_full_response(pkt["_non_stream_full"])
            if not res["tool_calls"] and res["content"] and on_delta is not None:
                emit_status(make_spoiler_safe_status(role, "writing"))
                on_delta(res["content"])
            return res

        ptype = pkt.get("type")
        if ptype == "error":
            err_obj = pkt.get("error") or {}
            raise RuntimeError(f"Anthropic 流式错误：{err_obj.get('message') or pkt}")
        elif ptype == "content_block_start":
            idx = int(pkt.get("index", 0) or 0)
            cb = pkt.get("content_block") or {}
            cb_type = cb.get("type")
            if cb_type == "thinking":
                _on_think()
                block_map[idx] = {"type": "thinking"}
            elif cb_type == "tool_use":
                if not saw_tool_call:
                    saw_tool_call = True
                    pre_buf.clear()
                    if flushed_to_ui and on_step_reset:
                        on_step_reset()
                init_inp = cb.get("input")
                init_json = json.dumps(init_inp, ensure_ascii=False) if isinstance(init_inp, dict) and init_inp else ""
                block_map[idx] = {
                    "type": "tool_use",
                    "id": cb.get("id") or f"toolu_{idx}",
                    "name": cb.get("name") or "",
                    "json_parts": [init_json] if init_json else [],
                }
            else:
                block_map[idx] = {"type": "text"}
        elif ptype == "content_block_delta":
            idx = int(pkt.get("index", 0) or 0)
            d = pkt.get("delta") or {}
            dtype = d.get("type")
            if dtype == "thinking_delta":
                if d.get("thinking"):
                    reasoning_parts.append(str(d["thinking"]))
                    _on_think()
            elif dtype == "text_delta":
                t_chunk = str(d.get("text") or "")
                if t_chunk:
                    raw_content_parts.append(t_chunk)
                    vis = stripper.feed(t_chunk)
                    if vis:
                        _push_visible(vis)
            elif dtype == "input_json_delta":
                b_entry = block_map.get(idx)
                if b_entry and b_entry.get("type") == "tool_use" and d.get("partial_json"):
                    b_entry["json_parts"].append(str(d["partial_json"]))
        elif ptype == "message_delta":
            md = pkt.get("delta") or {}
            if md.get("stop_reason"):
                finish_reason = md["stop_reason"]
            if pkt.get("usage"):
                usage = pkt["usage"]
        elif ptype == "message_stop":
            break

    if not finish_reason:
        raise RuntimeError("Anthropic 流式响应在返回结束状态前中断")
    tail_vis = stripper.finish()
    if tail_vis and not saw_tool_call:
        _push_visible(tail_vis)

    tcalls_list = []
    for k in sorted(block_map.keys()):
        b = block_map[k]
        if b.get("type") == "tool_use":
            joined_args = "".join(b.get("json_parts") or []) or "{}"
            tcalls_list.append({
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": joined_args},
            })

    if tcalls_list:
        if flushed_to_ui and on_step_reset:
            on_step_reset()
        pre_buf.clear()
    elif pre_buf and on_delta is not None:
        emit_status(make_spoiler_safe_status(role, "writing"))
        on_delta("".join(pre_buf))
        pre_buf.clear()

    if stripper.thinking_text:
        reasoning_parts.append(stripper.thinking_text)
    full_raw = "".join(raw_content_parts)
    clean_final, _ = strip_think_blocks(full_raw)
    return {
        "content": clean_final,
        "raw_content": full_raw,
        "reasoning": "\n".join(p for p in reasoning_parts if p),
        "tool_calls": tcalls_list,
        "usage": usage,
        "finish_reason": finish_reason,
    }


def verify_api_driver(driver_cfg: dict, timeout: float = 18.0) -> dict:
    """对当前配置的 Direct API（默认走通用 OpenAI `/chat/completions`，兼容 Anthropic 原生协议）执行轻量级连通性探针。"""
    api_fmt = _detect_api_format(driver_cfg)
    api_key = resolve_api_key(driver_cfg)
    api_base = _credential_endpoint(driver_cfg)
    model = (driver_cfg.get("model") or ("claude-sonnet-5-5" if api_fmt == "anthropic" else "deepseek-chat")).strip()

    is_local = is_local_api(api_base)
    if not api_key and not is_local:
        raise ValueError("尚未填写 API Key（也未检测到对应环境变量）。请先输入 API Key 或切换为本地免密 Ollama / CLI / Harness。")

    t0 = time.perf_counter()
    if api_fmt == "anthropic":
        url = _anthropic_messages_url(api_base)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Reply with OK"}],
        }
        data = _post_json(url, headers, payload, timeout=timeout)
        parsed = _parse_anthropic_full_response(data)
        echo_model = data.get("model") or model
        reply = parsed["content"]
        reason = parsed["finish_reason"]
    else:
        url = _openai_chat_url(api_base)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        m_tail = model.lower().rsplit("/", 1)[-1]
        tok_key = "max_completion_tokens" if m_tail.startswith(("o1", "o3", "o4", "gpt-5")) else "max_tokens"
        payload = {
            "model": model,
            tok_key: 256,
            "messages": [{"role": "user", "content": "Reply with OK"}],
        }
        try:
            data = _post_json(url, headers, payload, timeout=timeout)
        except Exception as probe_exc:
            err_s = str(probe_exc)
            if "(400)" in err_s and ("max_tokens" in err_s or "max_completion_tokens" in err_s):
                alt_payload = {"model": model, "messages": [{"role": "user", "content": "Reply with OK"}]}
                data = _post_json(url, headers, alt_payload, timeout=timeout)
            else:
                raise
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        reply, _ = strip_think_blocks(msg.get("content") or "")
        reason = choice.get("finish_reason")
        echo_model = data.get("model") or model

    if reply.strip().rstrip(".。!！").upper() != "OK" or reason not in (None, "", "stop", "end_turn", "stop_sequence"):
        raise ValueError("API 没有返回预期的探针回复，请检查模型与接口协议")
    reply_preview = reply.strip()[:48]

    ms = round((time.perf_counter() - t0) * 1000, 1)
    log_event("driver.verify", mode="api", protocol=api_fmt, api_base=api_base, model=echo_model, ms=ms, ok=True)
    return {
        "ok": True,
        "mode": "api",
        "protocol": api_fmt,
        "api_base": api_base,
        "model": echo_model,
        "latency_ms": ms,
        "reply_preview": reply_preview,
        "detail": f"API 连通性检查通过（协议：{api_fmt} · 模型：{echo_model} · 耗时 {ms}ms）",
    }


def run_openai_compat_loop(
    driver_cfg,
    system_prompt,
    messages,
    sd,
    role="front",
    world_sim_callback=None,
    tuner_context=None,
    max_steps=8,
    stream=False,
    on_status=None,
    on_delta=None,
    on_step_reset=None,
    tools=None,
    tool_guard=None,
):
    """通过 Direct API（优先走通用 OpenAI `/chat/completions`，兼容 Anthropic 原生 `/v1/messages`）执行多步 Tool Calling 循环。

    tool_guard：调用方想事后知道写回成败时传入自己的 ToolRunGuard；不传也会建一个，熔断照样生效。
    """
    tool_guard = tool_guard if tool_guard is not None else ToolRunGuard()
    api_fmt = _detect_api_format(driver_cfg)
    api_key = resolve_api_key(driver_cfg)
    api_base = _credential_endpoint(driver_cfg)
    model = driver_cfg.get("model") or ("claude-sonnet-5-5" if api_fmt == "anthropic" else "deepseek-chat")
    if not api_key and not is_local_api(api_base):
        raise RuntimeError(
            f"未配置 API Key（请在开局向导或顶部「⚙️ 驱动与提示词」中填入 api_key，或设置环境变量 {driver_cfg.get('api_key_env', 'DEEPSEEK_API_KEY')}）"
        )

    role_tools_map = {
        "front": FRONT_TOOLS_OPENAI,
        "back": BACK_TOOLS_OPENAI,
        "tuner": TUNER_TOOLS_OPENAI,
        "genesis": GENESIS_TOOLS_OPENAI + [TUNER_TOOLS_OPENAI[1]],
    }
    if tools is None:
        tools = role_tools_map.get(role, FRONT_TOOLS_OPENAI)
    allowed_tool_names = {
        t["function"]["name"]
        for t in (tools or [])
        if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")
    }
    anth_tools = _convert_tools_to_anthropic(tools) if api_fmt == "anthropic" and tools else None
    effort_fields = {} if (api_base, model) in _NO_EFFORT_PARAMS else api_effort_fields(api_fmt, api_base, model, driver_cfg.get("effort"))
    chat_msgs = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + list(messages)
    tool_logs = []

    if api_fmt == "anthropic":
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        url = _anthropic_messages_url(api_base)
    else:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = _openai_chat_url(api_base)

    temp = 0.45 if role in ("tuner", "genesis") else 0.75
    omit_temp = _omits_temperature(model) or ((api_base, model) in _NO_TEMP_MODELS)
    is_streaming_req = bool(stream or on_status is not None)
    step_timeout = resolve_api_timeout(driver_cfg, role=role, streaming=is_streaming_req)

    last_phase_key = None

    def emit_status(st_dict):
        nonlocal last_phase_key
        if not on_status or not isinstance(st_dict, dict):
            return
        pk = (st_dict.get("phase"), st_dict.get("label"))
        if pk != last_phase_key:
            last_phase_key = pk
            on_status(st_dict)

    effective_delta = on_delta if (stream and role in ("front", "tuner")) else None

    for step_idx in range(1, max_steps + 1):
        if api_fmt == "anthropic":
            anth_sys, anth_msgs = _convert_messages_to_anthropic(chat_msgs)
            sys_payload = (
                [{"type": "text", "text": anth_sys, "cache_control": {"type": "ephemeral"}}]
                if anth_sys
                else ""
            )
            payload = {
                "model": model,
                "max_tokens": int(driver_cfg.get("max_tokens") or 16000),
                "messages": anth_msgs,
            }
            if sys_payload:
                payload["system"] = sys_payload
            if anth_tools:
                payload["tools"] = anth_tools
        else:
            payload = {
                "model": model,
                "messages": chat_msgs,
            }
            if not omit_temp:
                payload["temperature"] = temp
            if tools:
                payload["tools"] = tools
            if driver_cfg.get("max_tokens"):
                tok_k = "max_completion_tokens" if model.lower().rsplit("/", 1)[-1].startswith(("o1", "o3", "o4", "gpt-5")) else "max_tokens"
                payload[tok_k] = int(driver_cfg["max_tokens"])
        payload.update(effort_fields)

        step_t0 = time.perf_counter()
        message_sizes = [(m.get("role"), len(m.get("content") or "")) for m in chat_msgs]
        log_event(
            "llm.req",
            role=role,
            step=step_idx,
            protocol=api_fmt,
            model=model,
            api_base=api_base,
            stream=is_streaming_req,
            msg_count=len(chat_msgs),
            sys_chars=len(system_prompt or ""),
            message_chars=sum(size for _, size in message_sizes),
            tool_reply_chars=sum(size for msg_role, size in message_sizes if msg_role == "tool"),
            replayed_reasoning_chars=sum(len(m.get("reasoning_content") or "") for m in chat_msgs),
            tools_offered=len(tools or []),
            effort_fields=list(effort_fields),
        )

        emit_status(make_spoiler_safe_status(role, "thinking"))

        def _send(pl):
            if api_fmt == "anthropic":
                if is_streaming_req:
                    return _execute_anthropic_step_streaming(
                        url,
                        headers,
                        pl,
                        role=role,
                        emit_status=emit_status,
                        on_delta=effective_delta,
                        on_step_reset=on_step_reset if role == "front" else None,
                        timeout=step_timeout,
                    )
                return _parse_anthropic_full_response(_post_json(url, headers, pl, timeout=step_timeout))
            if is_streaming_req:
                return _execute_step_streaming(
                    url,
                    headers,
                    pl,
                    role=role,
                    emit_status=emit_status,
                    on_delta=effective_delta,
                    on_step_reset=on_step_reset if role == "front" else None,
                    timeout=step_timeout,
                )
            data = _post_json(url, headers, pl, timeout=step_timeout)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content, inline_th = strip_think_blocks(msg.get("content") or "")
            has_api_r = ("reasoning_content" in msg and msg.get("reasoning_content") is not None) or ("reasoning" in msg and msg.get("reasoning") is not None)
            r_api = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
            return {
                "content": content,
                "reasoning": "\n".join(x for x in (r_api, inline_th) if x),
                "api_reasoning": r_api,
                "has_api_reasoning": has_api_r,
                "tool_calls": msg.get("tool_calls") or [],
                "usage": data.get("usage"),
                "finish_reason": choice.get("finish_reason") or "",
            }

        request_attempts = 1
        try:
            try:
                step_res = _send(payload)
            except Exception as first_exc:
                err_txt = str(first_exc)
                retried = False
                if "(400)" in err_txt:
                    if effort_fields:
                        log_event("llm.effort_unsupported", lvl="WARN", role=role, model=model, api_base=api_base, fields=list(effort_fields), err=err_txt[:300])
                        _NO_EFFORT_PARAMS.add((api_base, model))
                        for k in effort_fields:
                            payload.pop(k, None)
                        effort_fields = {}
                        retried = True
                    if "temperature" in payload and "temperature" in err_txt.lower():
                        _NO_TEMP_MODELS.add((api_base, model))
                        omit_temp = True
                        payload.pop("temperature", None)
                        retried = True
                if not retried:
                    raise
                request_attempts += 1
                step_res = _send(payload)
            clean_content = step_res["content"]
            reasoning_text = step_res["reasoning"]
            tcalls = step_res["tool_calls"]
            usage = step_res["usage"]
            finish_reason = step_res["finish_reason"]
        except Exception as api_exc:
            log_error(
                "llm.err",
                exc=api_exc,
                role=role,
                step=step_idx,
                protocol=api_fmt,
                model=model,
                api_base=api_base,
                request_attempts=request_attempts,
                ms=round((time.perf_counter() - step_t0) * 1000, 1),
            )
            raise

        step_ms = round((time.perf_counter() - step_t0) * 1000, 1)
        if reasoning_text:
            log_event(
                "llm.think",
                role=role,
                step=step_idx,
                model=model,
                chars=len(reasoning_text),
                reasoning=reasoning_text,
            )

        log_event(
            "llm.resp",
            role=role,
            step=step_idx,
            protocol=api_fmt,
            model=model,
            ms=step_ms,
            finish_reason=finish_reason,
            request_attempts=request_attempts,
            content_chars=len(clean_content),
            content_preview=clean_content[:400],
            tool_calls=[((tc.get("function") or {}).get("name", "")) for tc in tcalls],
            usage=usage,
        )

        assistant_msg = {"role": "assistant", "content": clean_content or ""}
        if tcalls:
            assistant_msg["tool_calls"] = tcalls
        # 严格对齐 OpenAI 与 DeepSeek Reasoner 协议：仅当接口实际返回了 reasoning_content 或处于 reasoner 模型时回传该字段，
        # 避免将正文内的 `<think>` 伪装成 `reasoning_content` 导致严格校验的 OpenAI / Claude 兼容网关报 400 Extra inputs
        if tcalls and api_fmt == "openai" and (step_res.get("has_api_reasoning") or "reasoner" in model.lower()):
            assistant_msg["reasoning_content"] = step_res.get("api_reasoning") or ""
        chat_msgs.append(assistant_msg)

        if finish_reason in ("length", "max_tokens", "content_filter", "refusal", "pause_turn"):
            raise RuntimeError(f"模型没有完成本轮输出（{finish_reason}）")
        if finish_reason in ("tool_calls", "tool_use", "function_call") and not tcalls:
            raise RuntimeError("模型结束时声明了工具调用，但没有返回可执行的调用")
        if not tcalls:
            if not clean_content.strip():
                raise RuntimeError("模型没有返回最终回复")
            return {
                "text": clean_content,
                "tool_logs": tool_logs,
                "model": model,
            }

        for tc_i, tc in enumerate(tcalls, 1):
            fn = (tc.get("function") or {}).get("name", "")
            call_id = tc.get("id") or f"call_{step_idx}_{tc_i}"
            if fn not in allowed_tool_names:
                log_event("tool.acl_deny", lvl="WARN", role=role, tool=fn, allowed=sorted(allowed_tool_names))
                chat_msgs.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"✗ 当前阶段未开放工具 `{fn}`（当前仅允许：{', '.join(sorted(allowed_tool_names)) or '无工具'}）。请勿调用未授权工具，直接按本阶段要求完成输出。",
                })
                continue

            raw_args = (tc.get("function") or {}).get("arguments") or "{}"
            parsed_args, parse_err = _parse_tool_arguments(raw_args)
            if parse_err is not None:
                log_event("tool.bad_json", lvl="WARN", role=role, tool=fn, raw_args=raw_args, err=parse_err)
                chat_msgs.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"✗ 工具 `{fn}` 的参数 JSON 格式不合法，未执行：{parse_err}。请检查引号与括号转义后重新调用。",
                })
                continue

            emit_status(make_spoiler_safe_status(role, "tool", fn))
            result_str = execute_tool(
                sd,
                fn,
                parsed_args,
                role=role,
                world_sim_callback=world_sim_callback,
                tuner_context=tuner_context,
                guard=tool_guard,
            )
            tool_logs.append({"tool": fn, "status": "done"})
            chat_msgs.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result_str,
            })

    log_event("llm.max_steps_reached", lvl="WARN", role=role, max_steps=max_steps)
    raise RuntimeError(f"模型达到工具调用步数上限（{max_steps}），未返回最终回复")


run_api_loop = run_openai_compat_loop
