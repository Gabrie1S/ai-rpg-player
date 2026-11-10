"""AI-First 结构化诊断日志模块（JSONL Machine Telemetry for AI Debugging）。

设计目标：
- 专为 AI 编程助手（Claude Code / Codex / Antigravity）排查线上/本地运行故障而设计，无需迁就人类阅读排版。
- 采用单行紧凑 JSONL 协议，携带跨线程/跨子进程的全局追踪号 `tid`（通过 ContextVar + 环境变量 `RPG_TRACE_ID` 自动传播）。
- 双流分拣落盘（位于 `<ROOT>/logs/`，已被 `.gitignore` 忽略）：
  1. `logs/engine.jsonl` ：全量关键事件流（回合输入、上下文装配、模型原始思考链 reasoning/think、工具参数与返回、幕后推演、文件事务、耗时、状态流转），超 10MB 自动轮转；
  2. `logs/errors.jsonl` ：所有 `WARN` / `ERR` 异常、Schema 校验失败、事务回滚、HTTP 4xx/5xx 与堆栈专索引流，AI 排查问题只需先读此文件定位 `tid`，再用 `--trace <tid>` 还原完整现场。
- 自动脱敏 `api_key`、`Authorization`、`Bearer ...`、`sk-...` 等密钥信息，且任何磁盘写入异常静默兜底绝不影响主流程。

AI 快速排查命令：
  python engine/tools/ai_log.py --errors -n 20
  python engine/tools/ai_log.py --trace <tid>
  python engine/tools/ai_log.py --save <存档名> --ev turn.outcome -n 10
  python engine/tools/ai_log.py --tail 50
  python engine/tools/ai_log.py --ev llm. --tail 20
  python engine/tools/ai_log.py --stats
"""
import argparse
from collections import deque
import contextlib
import contextvars
import datetime
import itertools
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT  # noqa: E402

LOGS_DIR = ROOT / "logs"
ENGINE_LOG_PATH = LOGS_DIR / "engine.jsonl"
ERRORS_LOG_PATH = LOGS_DIR / "errors.jsonl"

MAX_ENGINE_BYTES = 10 * 1024 * 1024   # 10 MB 自动轮转
MAX_ERRORS_BYTES = 5 * 1024 * 1024    # 5 MB 自动轮转
MAX_ROTATED_FILES = 3
MAX_STR_LEN = 16000                   # 单字段最大保留字符数（足够容纳完整思考链与堆栈）

_WRITE_LOCK = threading.Lock()
_SEQ_COUNTER = itertools.count(1)

_SECRET_KEYS = {"api_key", "apikey", "authorization", "token", "secret", "password", "access_token"}
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE)
_SK_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]{6,}\b")

_TRACE_VAR: contextvars.ContextVar[dict] = contextvars.ContextVar("rpg_trace_state", default={})


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_trace_id(prefix: str = "t") -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def get_trace_state() -> dict:
    st = _TRACE_VAR.get()
    if st:
        return dict(st)
    env_tid = os.environ.get("RPG_TRACE_ID", "").strip()
    if env_tid:
        ctx = {}
        raw_ctx = os.environ.get("RPG_TRACE_CTX", "").strip()
        if raw_ctx:
            try:
                ctx = json.loads(raw_ctx)
            except Exception:
                ctx = {}
        return {"tid": env_tid, "ctx": ctx}
    return {}


def current_trace_id() -> str:
    return get_trace_state().get("tid", "")


@contextlib.contextmanager
def trace_scope(op: str, tid: str = None, **ctx_fields):
    """开启或继承一段追踪上下文（支持嵌套与跨线程复制 ContextVar）。"""
    prev = get_trace_state()
    active_tid = tid or prev.get("tid") or new_trace_id(prefix=op[:4] or "t")
    merged_ctx = dict(prev.get("ctx") or {})
    merged_ctx["op"] = op
    for k, v in ctx_fields.items():
        if v is not None and v != "":
            merged_ctx[k] = v
    token = _TRACE_VAR.set({"tid": active_tid, "ctx": merged_ctx})
    t0 = time.perf_counter()
    log_event(f"{op}.begin", lvl="INFO", **ctx_fields)
    try:
        yield active_tid
    except Exception as exc:
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        log_error(f"{op}.unhandled_exc", exc=exc, ms=elapsed)
        raise
    else:
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        log_event(f"{op}.end", lvl="INFO", ms=elapsed)
    finally:
        _TRACE_VAR.reset(token)


def bind_trace_state(tid: str, ctx: dict = None):
    """供流式生成器在不同线程/协程迭代恢复时显式绑定追踪号。"""
    return _TRACE_VAR.set({"tid": tid, "ctx": dict(ctx or {})})


def reset_trace_state(token):
    try:
        _TRACE_VAR.reset(token)
    except Exception:
        pass


def _norm_path_entry(entry: str) -> str:
    cleaned = (entry or "").strip().strip('"')
    return os.path.normcase(os.path.normpath(cleaned)) if cleaned else ""


def child_env_with_trace(base_env: dict = None) -> dict:
    """构造带 `RPG_TRACE_ID` 与当前 Python 环境 PATH 的子进程环境变量，确保子进程工具调用始终优先命中当前解释器。"""
    env = dict(base_env if base_env is not None else os.environ)
    py_bin_dir = str(Path(sys.executable).resolve().parent)
    cur_path = env.get("PATH") or ""
    path_parts = [p for p in cur_path.split(os.pathsep) if p] if cur_path else []
    if py_bin_dir:
        norm_target = _norm_path_entry(py_bin_dir)
        rest = [p for p in path_parts if _norm_path_entry(p) != norm_target]
        env["PATH"] = os.pathsep.join([py_bin_dir] + rest)
    venv_root = Path(sys.executable).resolve().parents[1]
    if (venv_root / "pyvenv.cfg").is_file():
        env["VIRTUAL_ENV"] = str(venv_root)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    st = get_trace_state()
    if st.get("tid"):
        env["RPG_TRACE_ID"] = st["tid"]
        if st.get("ctx"):
            try:
                env["RPG_TRACE_CTX"] = json.dumps(st["ctx"], ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
    return env


def _scrub_str(s: str, max_len: int = MAX_STR_LEN) -> str:
    s = _BEARER_RE.sub(r"\1***REDACTED***", s)
    s = _SK_KEY_RE.sub(r"\1***REDACTED***", s)
    if len(s) > max_len:
        omitted = len(s) - max_len
        return f"{s[:max_len]}...[TRUNC:{omitted}_CHARS]"
    return s


def sanitize_payload(obj, depth: int = 0):
    """递归脱敏密钥并安全序列化任意 Python 对象。"""
    if depth > 8:
        return "<MAX_DEPTH>"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if ks.lower() in _SECRET_KEYS:
                out[ks] = "***REDACTED***" if v else ""
            else:
                out[ks] = sanitize_payload(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set)):
        seq = list(obj)
        if len(seq) > 200:
            return [sanitize_payload(x, depth + 1) for x in seq[:200]] + [f"...[+{len(seq) - 200}_ITEMS]"]
        return [sanitize_payload(x, depth + 1) for x in seq]
    return _scrub_str(repr(obj))


def _rotate_if_needed(path: Path, max_bytes: int):
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        oldest = path.with_suffix(f".{MAX_ROTATED_FILES}.jsonl")
        if oldest.exists():
            oldest.unlink()
        for idx in range(MAX_ROTATED_FILES - 1, 0, -1):
            src = path.with_suffix(f".{idx}.jsonl")
            dst = path.with_suffix(f".{idx + 1}.jsonl")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_suffix(".1.jsonl"))
    except Exception:
        pass


def log_event(ev: str, lvl: str = "INFO", tid: str = None, **data) -> str:
    """写入单行紧凑 JSONL 诊断事件，返回该事件所属的 `tid`。任何 IO 异常均被静默吸收。"""
    try:
        st = get_trace_state()
        active_tid = tid or st.get("tid") or new_trace_id("evt")
        lvl_up = (lvl or "INFO").upper()
        record = {
            "ts": _utc_iso(),
            "pid": os.getpid(),
            "seq": next(_SEQ_COUNTER),
            "lvl": lvl_up,
            "tid": active_tid,
            "ev": ev,
        }
        if st.get("ctx"):
            record["ctx"] = sanitize_payload(st["ctx"])
        if data:
            record["data"] = sanitize_payload(data)

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _WRITE_LOCK:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(ENGINE_LOG_PATH, MAX_ENGINE_BYTES)
            with open(ENGINE_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
            if lvl_up in ("WARN", "WARNING", "ERR", "ERROR", "FATAL"):
                _rotate_if_needed(ERRORS_LOG_PATH, MAX_ERRORS_BYTES)
                with open(ERRORS_LOG_PATH, "a", encoding="utf-8") as efh:
                    efh.write(line)
        return active_tid
    except Exception:
        return tid or ""


def log_error(ev: str, exc: BaseException = None, tid: str = None, **data) -> str:
    """记录异常或校验错误（自动附带异常类型、消息与完整 Python 堆栈），同时写入 engine.jsonl 与 errors.jsonl。"""
    payload = dict(data)
    if exc is not None:
        payload["exc_type"] = type(exc).__name__
        payload["exc_msg"] = str(exc)
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        payload["tb"] = tb_str
    elif "tb" not in payload and sys.exc_info()[0] is not None:
        payload["tb"] = traceback.format_exc()
    return log_event(ev, lvl="ERR", tid=tid, **payload)


# ==================== 供 AI Agent 使用的命令行日志检索器 ====================

def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw), raw
            except Exception:
                continue


def query_logs(errors_only=False, trace_id="", ev_prefix="", limit=50, save_name=""):
    target = ERRORS_LOG_PATH if errors_only else ENGINE_LOG_PATH
    # 过滤检索跨越所有仍保留的轮转文件，最老的先读。
    candidates = [target.with_suffix(f".{i}.jsonl") for i in range(MAX_ROTATED_FILES, 0, -1)] if (trace_id or save_name) else []
    candidates.append(target)

    matched = deque(maxlen=limit if limit and limit > 0 and not trace_id else None)
    for p in candidates:
        for obj, raw in _iter_jsonl(p):
            if trace_id and obj.get("tid") != trace_id:
                continue
            if ev_prefix and not str(obj.get("ev", "")).startswith(ev_prefix):
                continue
            if save_name and save_name not in ((obj.get("ctx") or {}).get("save"), (obj.get("data") or {}).get("save")):
                continue
            matched.append(raw)
    return list(matched)


def compute_stats():
    ev_counts = {}
    lvl_counts = {}
    recent_err_tids = []
    for obj, _ in _iter_jsonl(ENGINE_LOG_PATH):
        ev = obj.get("ev", "unknown")
        lvl = obj.get("lvl", "INFO")
        ev_counts[ev] = ev_counts.get(ev, 0) + 1
        lvl_counts[lvl] = lvl_counts.get(lvl, 0) + 1
    for obj, _ in _iter_jsonl(ERRORS_LOG_PATH):
        t = obj.get("tid")
        if t and t not in recent_err_tids:
            recent_err_tids.append(t)
    return {
        "engine_log": str(ENGINE_LOG_PATH),
        "engine_bytes": ENGINE_LOG_PATH.stat().st_size if ENGINE_LOG_PATH.exists() else 0,
        "errors_log": str(ERRORS_LOG_PATH),
        "errors_bytes": ERRORS_LOG_PATH.stat().st_size if ERRORS_LOG_PATH.exists() else 0,
        "level_counts": lvl_counts,
        "top_events": dict(sorted(ev_counts.items(), key=lambda kv: -kv[1])[:25]),
        "recent_error_trace_ids": recent_err_tids[-15:],
    }


def main():
    ap = argparse.ArgumentParser(description="AI 结构化诊断日志检索工具")
    ap.add_argument("--errors", action="store_true", help="仅读取 logs/errors.jsonl 异常与回滚流")
    ap.add_argument("--trace", default="", help="按 tid 追踪完整调用链（含跨子进程记录）")
    ap.add_argument("--ev", default="", help="按事件名前缀过滤，如 llm. / tool. / apply.")
    ap.add_argument("--save", default="", help="只看这个存档的日志（包括仍保留的轮转文件）")
    ap.add_argument("-n", "--tail", type=int, default=40, help="返回最近 N 条匹配记录（默认 40）")
    ap.add_argument("--stats", action="store_true", help="输出当前日志统计摘要 JSON")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(compute_stats(), ensure_ascii=False, indent=2))
        return 0

    lines = query_logs(
        errors_only=args.errors,
        trace_id=args.trace.strip(),
        ev_prefix=args.ev.strip(),
        limit=args.tail,
        save_name=args.save.strip(),
    )
    for ln in lines:
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
