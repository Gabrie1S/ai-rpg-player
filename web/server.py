"""AI-RPG-Player 轻量级解耦 Web 后端（支持 FastAPI + Python 标准库双擎无缝运行，原生支持 SSE 流式输出）。

架构定位：
- 本文件仅作为 engine/runtime/orchestrator.py 与 engine/tools/*.py 的薄 REST + SSE 适配层。
- 所有的游戏状态、校验、上下文预装配、防剧透流式过滤、按需调教 Sub-Agent 与三阶段创世向导均在 engine/ 内完成。
- 若环境已安装 FastAPI + Uvicorn，自动以 FastAPI 启动；若未安装，自动无缝回退至 Python 标准库 ThreadingHTTPServer，零额外依赖即可打开浏览器流式游玩。

启动方式（在项目根目录）：
  python web/server.py
"""
import json
import os
import mimetypes
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine" / "tools"))

from ai_log import log_error, log_event  # noqa: E402
from common import load_config, save_config, save_dir  # noqa: E402
from engine.runtime.driver_config import driver_fingerprint, driver_log_meta, merge_driver_config, resolve_back_driver  # noqa: E402
from engine.runtime.orchestrator import RPGOrchestrator  # noqa: E402
from newsave import create_save, set_current  # noqa: E402
from pack_io import export_novel, export_pack, export_seed, list_packs  # noqa: E402

orchestrator = RPGOrchestrator()
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ==================== 纯 Python 路由核心（与 Web 框架解耦） ====================

def handle_get_state(save_name=None):
    return orchestrator.get_hud_state(save_name)


def handle_story_page(save_name=None, before=None, limit=None):
    return orchestrator.get_story_page(save_name, before, limit)


def handle_system_selfcheck():
    return orchestrator.run_system_selfcheck()


def handle_driver_verify(body):
    return orchestrator.verify_driver_and_save(body or {})


def handle_pack_import_upload(body):
    return orchestrator.import_pack_upload(body or {})


def handle_list_packs():
    return {"packs": list_packs()}


def handle_get_pack_detail(pack_id):
    pid = (pack_id or "").strip()
    if not pid:
        raise ValueError("pack_id 不能为空")
    return orchestrator.get_pack_detail(pid)


def handle_pack_doctor(pack_id):
    pid = (pack_id or "").strip()
    if not pid:
        raise ValueError("pack_id 不能为空")
    return orchestrator.run_pack_doctor(pid)


def handle_pack_flatten(body):
    pid = (body.get("pack_id") or "").strip()
    if not pid:
        raise ValueError("pack_id 不能为空")
    return orchestrator.run_pack_flatten(pid)


def handle_select_pack(body):
    orchestrator.refuse_while_job()
    pid = (body.get("pack_id") or "").strip()
    if not pid:
        raise ValueError("pack_id 不能为空")
    auto_sw = bool(body.get("auto_switch_save", True))
    return orchestrator.select_pack(pid, auto_switch_save=auto_sw)


def handle_create_save(body):
    orchestrator.refuse_while_job()
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("存档名不能为空")
    create_save(name, body.get("pack_id"), body.get("setup_answers"))
    return {"ok": True, "hud": orchestrator.get_hud_state(name)}


def handle_select_save(body):
    orchestrator.refuse_while_job()
    name = (body.get("name") or "").strip()
    sd = save_dir(name, required=False)
    if not sd:
        raise ValueError(f"存档不存在：{name}")
    set_current(sd.name)
    return {"ok": True, "hud": orchestrator.get_hud_state(sd.name)}


def handle_run_turn(body):
    inp = (body.get("input") or "").strip()
    if not inp:
        raise ValueError("输入不能为空")
    return orchestrator.run_turn(inp, body.get("save_name"))


def handle_swipe_turn(body):
    return orchestrator.swipe_last_turn(body.get("feedback", ""), body.get("save_name"))


def handle_undo_turn(body):
    return orchestrator.undo_last_turn((body or {}).get("save_name"))


def handle_edit_story(body):
    return orchestrator.edit_latest_story(body.get("new_story", ""), body.get("save_name"))


def handle_style_example(body):
    excerpt = (body.get("excerpt") or "").strip()
    if not excerpt:
        raise ValueError("选中的范例段落不能为空")
    return orchestrator.add_style_example(
        excerpt,
        bool(body.get("liked", True)),
        body.get("reason", ""),
        body.get("save_name"),
    )


def handle_tuner_chat(body):
    msg = (body.get("message") or "").strip()
    if not msg:
        raise ValueError("调教反馈内容不能为空")
    return orchestrator.run_tuner(msg, body.get("save_name"))


def handle_tuner_clear(body):
    return orchestrator.clear_tuner_history((body or {}).get("save_name"))


def handle_genesis_run(body):
    stage = (body.get("stage") or "").strip()
    if not stage:
        raise ValueError("缺少创世阶段参数 stage")
    return orchestrator.run_genesis_stage(stage, body)


# ==================== SSE 流式事件生成器（防剧透状态指示 + 实时打字机） ====================

def _encode_sse_frame(obj: dict) -> bytes:
    return (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")


def stream_sse_turn(body):
    try:
        inp = (body.get("input") or "").strip()
        if not inp:
            raise ValueError("输入不能为空")
        for ev in orchestrator.stream_turn(inp, body.get("save_name")):
            yield _encode_sse_frame(ev)
    except (ValueError, SystemExit) as e:
        tid = log_error("http.stream_bad_req", exc=e, route="/api/turn/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})
    except Exception as e:
        tid = log_error("http.stream_err", exc=e, route="/api/turn/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})


def stream_sse_swipe(body):
    try:
        for ev in orchestrator.stream_swipe_turn(body.get("feedback", ""), body.get("save_name")):
            yield _encode_sse_frame(ev)
    except (ValueError, SystemExit) as e:
        tid = log_error("http.stream_bad_req", exc=e, route="/api/turn/swipe/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})
    except Exception as e:
        tid = log_error("http.stream_err", exc=e, route="/api/turn/swipe/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})


def stream_sse_tuner(body):
    try:
        msg = (body.get("message") or "").strip()
        if not msg:
            raise ValueError("调教反馈内容不能为空")
        for ev in orchestrator.stream_tuner(msg, body.get("save_name")):
            yield _encode_sse_frame(ev)
    except (ValueError, SystemExit) as e:
        tid = log_error("http.stream_bad_req", exc=e, route="/api/tuner/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})
    except Exception as e:
        tid = log_error("http.stream_err", exc=e, route="/api/tuner/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})


def stream_sse_genesis(body):
    try:
        stage = (body.get("stage") or "").strip()
        if not stage:
            raise ValueError("缺少创世阶段参数 stage")
        for ev in orchestrator.stream_genesis_stage(stage, body):
            yield _encode_sse_frame(ev)
    except (ValueError, SystemExit) as e:
        tid = log_error("http.stream_bad_req", exc=e, route="/api/genesis/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})
    except Exception as e:
        tid = log_error("http.stream_err", exc=e, route="/api/genesis/stream")
        yield _encode_sse_frame({"type": "error", "detail": str(e), "trace_id": tid})


def handle_resolve_pending_world(body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("缺少要处理的世界")
    return orchestrator.resolve_pending_world(name, (body.get("action") or "").strip())


def handle_panel_source(body):
    return orchestrator.get_panel_source((body or {}).get("id"), (body or {}).get("save_name"))


def handle_remove_mod(body):
    return orchestrator.remove_mod((body or {}).get("path"), (body or {}).get("save_name"))


def handle_job_state():
    return orchestrator.get_job_state()


def handle_stream_cancel(body):
    return orchestrator.cancel_active_streams()


def handle_export(body):
    mode = body.get("mode", "")
    save_name = body.get("save_name")
    if mode == "pack":
        pid = body.get("pack_id") or load_config().get("active_pack", "")
        if not pid:
            raise ValueError("当前没有可导出的世界包")
        out = export_pack(pid)
        return {"ok": True, "mode": "pack", "path": str(out), "message": f"已导出纯净题材规则包：{out.name}"}
    elif mode == "seed":
        sd = save_dir(save_name)
        out = export_seed(sd)
        return {"ok": True, "mode": "seed", "path": str(out), "message": f"已静默导出脱敏世界母本种子（第0天开局态）：{out.name}"}
    elif mode in ("novel_md", "novel_html"):
        sd = save_dir(save_name)
        fmt = "html" if mode == "novel_html" else "md"
        out = export_novel(sd, fmt=fmt)
        return {"ok": True, "mode": mode, "path": str(out), "message": f"已导出跑团小说成品：{out.name}"}
    raise ValueError(f"未知导出模式：{mode}")


def handle_get_config():
    return orchestrator._mask_config(load_config())


def handle_update_config(body):
    orchestrator.refuse_while_job()  # 长任务结束时可能写配置，中途改的内容会被盖掉
    cfg = load_config()
    incoming = dict(body or {})
    verification_fields = ("driver_verified", "driver_verified_at", "driver_verified_for")
    for field in verification_fields:
        incoming.pop(field, None)
    for dk in ("front_driver", "back_driver"):
        if isinstance(incoming.get(dk), dict):
            existing_d = dict(cfg.get(dk) or {})
            incoming[dk] = merge_driver_config(existing_d, incoming[dk])
        elif dk in incoming:
            raise ValueError("驱动配置必须是字典")
    driver_changed = driver_fingerprint({**cfg, **incoming}) != driver_fingerprint(cfg)
    previous_front = driver_log_meta(cfg.get("front_driver") or {})
    previous_back = driver_log_meta(resolve_back_driver(cfg))
    was_verified = bool(cfg.get("driver_verified"))
    if driver_changed:
        for field in verification_fields:
            cfg.pop(field, None)
    cfg.update(incoming)
    save_config(cfg)
    log_event(
        "config.updated", driver_changed=driver_changed, verification_cleared=driver_changed and was_verified,
        before_front=previous_front, before_back=previous_back,
        front=driver_log_meta(cfg.get("front_driver") or {}), back=driver_log_meta(resolve_back_driver(cfg)),
    )
    return {"ok": True, "config": orchestrator._mask_config(cfg), "hud": orchestrator.get_hud_state()}


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

_LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


def _request_error(method, headers, scheme="http"):
    """Local browser boundary shared by both HTTP servers."""
    host = headers.get("Host", "")
    if not _LOCAL_HOST.fullmatch(host):
        return 403, "仅允许通过本机地址访问"
    origin = headers.get("Origin")
    if ((origin is not None and origin.lower() != f"{scheme}://{host.lower()}")
            or headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")):
        return 403, "不允许跨站访问本地跑团服务"
    if method == "POST" and headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
        return 415, "请求须使用 application/json"
    return None


# ==================== FastAPI 挂载（如已安装 FastAPI） ====================

try:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="AI-RPG-Player Engine API", version="1.2.0")
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        failure = _request_error(request.method, request.headers, request.url.scheme)
        if failure:
            status, detail = failure
            return JSONResponse({"detail": detail}, status_code=status)
        return await call_next(request)

    def _wrap(fn, *args):
        try:
            return fn(*args)
        except (ValueError, SystemExit) as e:
            log_error("http.bad_request", exc=e, fn=getattr(fn, "__name__", ""))
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            log_error("http.server_error", exc=e, fn=getattr(fn, "__name__", ""))
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/state")
    def api_get_state(save_name: str = None):
        return _wrap(handle_get_state, save_name)

    @app.get("/api/story")
    def api_story_page(save_name: str = None, before: int = None, limit: int = None):
        return _wrap(handle_story_page, save_name, before, limit)

    @app.get("/api/job")
    def api_job_state():
        return _wrap(handle_job_state)

    @app.get("/api/system/selfcheck")
    def api_system_selfcheck():
        return _wrap(handle_system_selfcheck)

    @app.post("/api/driver/verify")
    def api_driver_verify(body: dict = Body(default={})):
        return _wrap(handle_driver_verify, body)

    @app.post("/api/packs/import_upload")
    def api_pack_import_upload(body: dict = Body(...)):
        return _wrap(handle_pack_import_upload, body)

    @app.get("/api/packs")
    def api_list_packs():
        return _wrap(handle_list_packs)

    @app.get("/api/packs/detail")
    def api_get_pack_detail(pack_id: str = ""):
        return _wrap(handle_get_pack_detail, pack_id)

    @app.get("/api/packs/doctor")
    def api_pack_doctor(pack_id: str = ""):
        return _wrap(handle_pack_doctor, pack_id)

    @app.post("/api/packs/flatten")
    def api_pack_flatten(body: dict = Body(...)):
        return _wrap(handle_pack_flatten, body)

    @app.post("/api/packs/select")
    def api_select_pack(body: dict = Body(...)):
        return _wrap(handle_select_pack, body)

    @app.post("/api/saves/new")
    def api_create_save(body: dict = Body(...)):
        return _wrap(handle_create_save, body)

    @app.post("/api/saves/select")
    def api_select_save(body: dict = Body(...)):
        return _wrap(handle_select_save, body)

    @app.post("/api/turn")
    def api_run_turn(body: dict = Body(...)):
        return _wrap(handle_run_turn, body)

    @app.post("/api/turn/stream")
    def api_stream_turn(body: dict = Body(...)):
        return StreamingResponse(stream_sse_turn(body), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/turn/undo")
    def api_undo_turn(body: dict = Body(default={})):
        return _wrap(handle_undo_turn, body)

    @app.post("/api/turn/swipe")
    def api_swipe_turn(body: dict = Body(...)):
        return _wrap(handle_swipe_turn, body)

    @app.post("/api/turn/swipe/stream")
    def api_stream_swipe(body: dict = Body(...)):
        return StreamingResponse(stream_sse_swipe(body), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/story/edit")
    def api_edit_story(body: dict = Body(...)):
        return _wrap(handle_edit_story, body)

    @app.post("/api/style/example")
    def api_style_example(body: dict = Body(...)):
        return _wrap(handle_style_example, body)

    @app.post("/api/tuner/chat")
    def api_tuner_chat(body: dict = Body(...)):
        return _wrap(handle_tuner_chat, body)

    @app.post("/api/tuner/stream")
    def api_stream_tuner(body: dict = Body(...)):
        return StreamingResponse(stream_sse_tuner(body), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/tuner/clear")
    def api_tuner_clear(body: dict = Body(default={})):
        return _wrap(handle_tuner_clear, body)

    @app.post("/api/genesis/run")
    def api_genesis_run(body: dict = Body(...)):
        return _wrap(handle_genesis_run, body)

    @app.post("/api/genesis/stream")
    def api_stream_genesis(body: dict = Body(...)):
        return StreamingResponse(stream_sse_genesis(body), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/genesis/pending")
    def api_resolve_pending_world(body: dict = Body(...)):
        return _wrap(handle_resolve_pending_world, body)

    @app.post("/api/panels/source")
    def api_panel_source(body: dict = Body(...)):
        return _wrap(handle_panel_source, body)

    @app.post("/api/mods/remove")
    def api_remove_mod(body: dict = Body(...)):
        return _wrap(handle_remove_mod, body)

    @app.post("/api/stream/cancel")
    def api_stream_cancel(body: dict = Body(default={})):
        return _wrap(handle_stream_cancel, body)

    @app.post("/api/export")
    def api_export(body: dict = Body(...)):
        return _wrap(handle_export, body)

    @app.get("/api/config")
    def api_get_config():
        return _wrap(handle_get_config)

    @app.post("/api/config")
    def api_update_config(body: dict = Body(...)):
        return _wrap(handle_update_config, body)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    HAS_FASTAPI = True
except ImportError:
    app = None
    HAS_FASTAPI = False


# ==================== 标准库 HTTP Server 零依赖后备引擎 ====================

class StdlibRPGHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse_stream(self, frame_iter):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for frame_bytes in frame_iter:
                self.wfile.write(frame_bytes)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, file_path: Path):
        static_root = STATIC_DIR.resolve()
        try:
            resolved = file_path.resolve()
        except OSError:
            return self._send_json(404, {"detail": "Not Found"})
        if resolved != static_root and static_root not in resolved.parents:
            return self._send_json(403, {"detail": "Forbidden"})
        if not resolved.is_file():
            return self._send_json(404, {"detail": "Not Found"})
        mime, _ = mimetypes.guess_type(str(resolved))
        mime = mime or "application/octet-stream"
        is_text = mime.startswith("text/") or mime in ("application/json", "application/javascript", "application/xml", "image/svg+xml")
        ctype = f"{mime}; charset=utf-8" if is_text else mime
        raw = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        failure = _request_error("GET", self.headers)
        if failure:
            return self._send_json(failure[0], {"detail": failure[1]})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                rel = urllib.parse.unquote(path[len("/static/"):]).lstrip("/\\")
                return self._send_file(STATIC_DIR / rel)
            if path == "/api/state":
                sname = (qs.get("save_name") or [None])[0]
                return self._send_json(200, handle_get_state(sname))
            if path == "/api/story":
                first = lambda k: (qs.get(k) or [None])[0]  # noqa: E731
                return self._send_json(200, handle_story_page(first("save_name"), first("before"), first("limit")))
            if path == "/api/job":
                return self._send_json(200, handle_job_state())
            if path == "/api/system/selfcheck":
                return self._send_json(200, handle_system_selfcheck())
            if path == "/api/packs":
                return self._send_json(200, handle_list_packs())
            if path == "/api/packs/detail":
                pid = (qs.get("pack_id") or [""])[0]
                return self._send_json(200, handle_get_pack_detail(pid))
            if path == "/api/packs/doctor":
                pid = (qs.get("pack_id") or [""])[0]
                return self._send_json(200, handle_pack_doctor(pid))
            if path == "/api/config":
                return self._send_json(200, handle_get_config())
            self._send_json(404, {"detail": f"Unknown route: {path}"})
        except (ValueError, SystemExit) as e:
            log_error("http.bad_request", exc=e, route=path)
            self._send_json(400, {"detail": str(e)})
        except Exception as e:
            log_error("http.server_error", exc=e, route=path)
            self._send_json(500, {"detail": str(e)})

    def do_POST(self):
        failure = _request_error("POST", self.headers)
        if failure:
            return self._send_json(failure[0], {"detail": failure[1]})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024 * 1024:
            return self._send_json(413, {"detail": "上传请求体超过 64MB 安全上限"})
        raw = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else "{}"
        try:
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            return self._send_json(400, {"detail": "Invalid JSON"})

        stream_routes = {
            "/api/turn/stream": stream_sse_turn,
            "/api/turn/swipe/stream": stream_sse_swipe,
            "/api/tuner/stream": stream_sse_tuner,
            "/api/genesis/stream": stream_sse_genesis,
        }
        if path in stream_routes:
            return self._send_sse_stream(stream_routes[path](body))

        routes = {
            "/api/driver/verify": handle_driver_verify,
            "/api/packs/import_upload": handle_pack_import_upload,
            "/api/packs/flatten": handle_pack_flatten,
            "/api/packs/select": handle_select_pack,
            "/api/saves/new": handle_create_save,
            "/api/saves/select": handle_select_save,
            "/api/turn": handle_run_turn,
            "/api/turn/swipe": handle_swipe_turn,
            "/api/turn/undo": handle_undo_turn,
            "/api/story/edit": handle_edit_story,
            "/api/style/example": handle_style_example,
            "/api/stream/cancel": handle_stream_cancel,
            "/api/panels/source": handle_panel_source,
            "/api/mods/remove": handle_remove_mod,
            "/api/tuner/chat": handle_tuner_chat,
            "/api/tuner/clear": handle_tuner_clear,
            "/api/genesis/run": handle_genesis_run,
            "/api/genesis/pending": handle_resolve_pending_world,
            "/api/export": handle_export,
            "/api/config": handle_update_config,
        }
        fn = routes.get(path)
        if not fn:
            return self._send_json(404, {"detail": f"Unknown POST route: {path}"})
        try:
            return self._send_json(200, fn(body))
        except (ValueError, SystemExit) as e:
            log_error("http.bad_request", exc=e, route=path)
            return self._send_json(400, {"detail": str(e)})
        except Exception as e:
            log_error("http.server_error", exc=e, route=path)
            return self._send_json(500, {"detail": str(e)})


def run_server():
    engine_cfg = load_config()
    cfg = engine_cfg.get("web") or {}
    host = cfg.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        host = "127.0.0.1"
    # 启动器会在端口被占用时换一个，这里不改写玩家的配置
    port = int(os.environ.get("RPG_WEB_PORT") or cfg.get("port", 8000))
    quiet = bool(os.environ.get("RPG_QUIET"))
    log_event(
        "server.start", host=host, port=port, fastapi=HAS_FASTAPI, turn_receipts=True,
        front=driver_log_meta(engine_cfg.get("front_driver") or {}), back=driver_log_meta(resolve_back_driver(engine_cfg)),
    )
    if HAS_FASTAPI:
        try:
            import uvicorn
            print(f"🚀 AI-RPG-Player Web 引擎已启动 (FastAPI + SSE)：http://{host}:{port}")
            uvicorn.run(app, host=host, port=port, log_level="warning" if quiet else "info")
            return
        except ImportError:
            pass
    print(f"🚀 AI-RPG-Player Web 引擎已启动 (内置标准库 + SSE 双擎模式)：http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), StdlibRPGHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb 服务已停止。")


if __name__ == "__main__":
    run_server()
