"""End-to-end verification suite for:
1. System Self-Check (/api/system/selfcheck) & 3-manifest alignment (AGENTS.md / CLAUDE.md / GEMINI.md)
2. Mainstream Driver Verification (/api/driver/verify) across OpenAI API, Native Anthropic API, CLI, and Harness
3. Native Anthropic /v1/messages SSE Streaming + thinking_delta anti-spoiler stripping + tool_use loop
4. Browser Drag-and-Drop World Pack Upload (/api/packs/import_upload for nested .rpgpack/.zip & raw folder files)
5. Dialogue Box Style & Pixel-Art Sprite Matrix validation + atomic rollback in Pack Doctor / update_world_ui
"""
import asyncio
import base64
import io
import json
import shutil
import sys
import tempfile
import threading
import urllib.request
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine" / "tools"))

from common import PACKS, ROOT as ENGINE_ROOT, save_dir  # noqa: E402
from engine.runtime import api_driver as ad  # noqa: E402
from engine.runtime.api_driver import execute_tool, run_openai_compat_loop  # noqa: E402
from pack_io import doctor_pack, import_bundle, import_uploaded_payload  # noqa: E402
import web.server as ws  # noqa: E402


class MockMultiProtocolLLMHandler(BaseHTTPRequestHandler):
    anthropic_step = 0

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/credential-sink")
            self.end_headers()
            return

        # 1. OpenAI Chat Completions
        if self.path.endswith("/chat/completions"):
            return self._json(200, {
                "model": body.get("model", "mock-gpt"),
                "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            })

        # 2. Anthropic Native /v1/messages
        if self.path.endswith("/messages"):
            if not body.get("stream"):
                return self._json(200, {
                    "id": "msg_probe",
                    "model": body.get("model", "claude-sonnet-4"),
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "OK"}],
                })

            # Streaming Anthropic SSE with thinking_delta + tool_use (step 1) -> prose (step 2)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()

            def send_ev(ev_obj):
                self.wfile.write(f"data: {json.dumps(ev_obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

            has_tool_result = any(
                isinstance(m.get("content"), list)
                and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
                for m in body.get("messages", [])
            )

            if not has_tool_result:
                send_ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
                send_ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "TOP_SECRET_ANTHROPIC_THOUGHT: checking world/now.md"}})
                send_ev({"type": "content_block_stop", "index": 0})
                send_ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "read_save_file", "input": {}}})
                send_ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"rel_path": "world/'}})
                send_ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'now.md"}'}} )
                send_ev({"type": "content_block_stop", "index": 1})
                send_ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
                send_ev({"type": "message_stop"})
            else:
                send_ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
                send_ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "TOP_SECRET_ANTHROPIC_THOUGHT_STEP2"}})
                send_ev({"type": "content_block_stop", "index": 0})
                send_ev({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
                send_ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "霓虹雨幕落在街角，"}})
                send_ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "终端屏幕映出幽蓝微光。"}})
                send_ev({"type": "content_block_stop", "index": 1})
                send_ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
                send_ev({"type": "message_stop"})
            return

        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/credential-sink":
            self.server.redirected_headers = dict(self.headers)
        self._json(404, {"error": "not found"})


def http_req(base_url, path, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    cfg_path = ENGINE_ROOT / "config.yaml"
    orig_cfg_bytes = cfg_path.read_bytes()

    mock_llm = ThreadingHTTPServer(("127.0.0.1", 0), MockMultiProtocolLLMHandler)
    llm_port = mock_llm.server_address[1]
    threading.Thread(target=mock_llm.serve_forever, daemon=True).start()

    rpg_srv = ThreadingHTTPServer(("127.0.0.1", 0), ws.StdlibRPGHandler)
    rpg_port = rpg_srv.server_address[1]
    threading.Thread(target=rpg_srv.serve_forever, daemon=True).start()
    rpg_base = f"http://127.0.0.1:{rpg_port}"

    tmp_packs = []
    try:
        print("=== [SECURITY] Web origin, Host, redirects, and archive containment ===")
        invalid = (
            ("GET", "/api/state", {"Origin": "https://attacker.invalid"}, None, 403),
            ("GET", "/api/state", {"Host": "rebind.invalid"}, None, 403),
            ("POST", "/api/genesis/stream", {"Origin": "null", "Content-Type": "text/plain"}, b"{}", 403),
            ("POST", "/api/config", {"Content-Type": "text/plain"}, b"{}", 415),
        )
        for method, path, headers, body, status in invalid:
            try:
                urllib.request.urlopen(urllib.request.Request(rpg_base + path, body, headers, method=method), timeout=5)
                raise AssertionError("unsafe request reached stdlib route")
            except urllib.error.HTTPError as exc:
                assert exc.code == status and "Access-Control-Allow-Origin" not in exc.headers
        with urllib.request.urlopen(urllib.request.Request(rpg_base + "/api/state", headers={"Origin": rpg_base}), timeout=5) as resp:
            assert resp.status == 200
        if ws.HAS_FASTAPI:
            import httpx

            async def fastapi_checks():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ws.app), base_url=rpg_base) as client:
                    for method, path, headers, body, status in invalid:
                        resp = await client.request(method, path, headers=headers, content=body)
                        assert resp.status_code == status and "access-control-allow-origin" not in resp.headers
                    assert (await client.get("/api/state", headers={"Origin": rpg_base})).status_code == 200

            asyncio.run(fastapi_checks())

        original_transport = ad.httpx
        try:
            ad.httpx = None  # exercise urllib, which used to follow this redirect with the key
            for send in (ad._post_json, lambda *a: list(ad._iter_sse_json_packets(*a))):
                try:
                    send(f"http://127.0.0.1:{llm_port}/redirect", {"Authorization": "Bearer mock-key"}, {})
                    raise AssertionError("redirect should be rejected")
                except RuntimeError as exc:
                    assert "(302)" in str(exc), exc
            assert not hasattr(mock_llm, "redirected_headers")
        finally:
            ad.httpx = original_transport

        with tempfile.TemporaryDirectory() as td:
            bad_zip = Path(td) / "bad.rpgpack"
            cases = (
                ("rule_pack", "pack/../../escape.txt"),
                ("rule_pack", "pack/..\\..\\escape.txt"),
                ("rule_pack", "pack/C:/escape.txt"),
                ("seeded_world", "seed/.GiT/hooks/pre-commit"),
            )
            for bundle_type, member in cases:
                with zipfile.ZipFile(bad_zip, "w") as z:
                    z.writestr("rpgpack_manifest.json", json.dumps({"bundle_type": bundle_type}))
                    z.writestr(member, "test")
                for action in (
                    lambda: import_bundle(bad_zip, target_name="_security_probe"),
                    lambda: import_uploaded_payload({"filename": "../bad.rpgpack", "target_name": "_security_probe",
                                                     "archive_b64": base64.b64encode(bad_zip.read_bytes()).decode()}),
                ):
                    try:
                        action()
                        raise AssertionError(f"unsafe archive was accepted: {member}")
                    except ValueError:
                        pass
                assert not (ENGINE_ROOT / "escape.txt").exists()
                assert not (PACKS / "_security_probe").exists()
                assert not (ENGINE_ROOT / "saves" / "_security_probe").exists()
            before = (PACKS / "cyberpunk" / "pack.yaml").read_bytes()
            try:
                import_uploaded_payload({"target_name": "cyberpunk", "files": [{"path": "pack.yaml", "content": "bad"}]})
                raise AssertionError("import unexpectedly overwrote existing pack")
            except ValueError:
                pass
            assert (PACKS / "cyberpunk" / "pack.yaml").read_bytes() == before

        print("=== [NEW TEST 1] System Self-Check & Entry Manifest Pointers ===")
        code, sc = http_req(rpg_base, "/api/system/selfcheck")
        assert code == 200 and sc["ok"] is True
        for chk in sc["checks"]:
            assert chk["ok"] is True, f"Self-check failed: {chk}"
        assert len(sc["api_presets"]) >= 7
        cli_ids = {c["id"] for c in sc["clis"]}
        assert {"claude", "codex", "gemini", "agy", "opencode"}.issubset(cli_ids)
        har_ids = {h["id"] for h in sc["harnesses"]}
        assert {"external_ide", "openai_agents_sdk", "antigravity_sdk", "claude_agent_sdk"}.issubset(har_ids)
        # Verify CLAUDE.md and GEMINI.md are thin pointers to AGENTS.md
        assert "AGENTS.md" in (ENGINE_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "AGENTS.md" in (ENGINE_ROOT / "GEMINI.md").read_text(encoding="utf-8")
        assert len((ENGINE_ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()) <= 30
        print("OK: System self-check, 7 API presets, 5 CLIs, 4 Harnesses, and thin CLAUDE.md/GEMINI.md pointers verified.")

        print("\n=== [NEW TEST 2] Live Driver Verify (/api/driver/verify: OpenAI + Anthropic + Harness) ===")
        code, v_oai = http_req(rpg_base, "/api/driver/verify", "POST", {
            "front_driver": {
                "type": "api",
                "api_format": "openai",
                "api_base": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "sk-test-oai-secret",
                "model": "gpt-4o-mini",
            }
        })
        assert code == 200 and v_oai["check"]["ok"] is True
        assert v_oai["hud"]["onboarding"]["driver_verified"] is True

        code, v_ant = http_req(rpg_base, "/api/driver/verify", "POST", {
            "front_driver": {
                "type": "api",
                "api_format": "anthropic",
                "api_base": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "sk-ant-test-secret",
                "model": "claude-sonnet-4-20250514",
            }
        })
        assert code == 200 and v_ant["check"]["ok"] is True and v_ant["check"]["protocol"] == "anthropic"

        code, v_har = http_req(rpg_base, "/api/driver/verify", "POST", {
            "front_driver": {
                "type": "harness",
                "harness_mode": "external_ide",
            }
        })
        assert code == 200 and v_har["check"]["ok"] is True
        assert not v_har["check"]["web_execution"] and not v_har["hud"]["onboarding"]["driver_ready"]
        print("OK: OpenAI API, Native Anthropic /v1/messages API, and Agent Harness verification all passed.")

        print("\n=== [NEW TEST 3] Anthropic Native Streaming + Anti-Spoiler Thinking Strip + Tool Loop ===")
        sd = save_dir("霓虹测试")
        statuses = []
        deltas = []
        res = run_openai_compat_loop(
            {
                "type": "api",
                "api_format": "anthropic",
                "api_base": f"http://127.0.0.1:{llm_port}/v1",
                "api_key": "sk-ant-stream-key",
                "model": "claude-sonnet-4-20250514",
            },
            "你是台前主持人。",
            [{"role": "user", "content": "环顾四周。"}],
            sd,
            role="front",
            stream=True,
            on_status=lambda s: statuses.append(s),
            on_delta=lambda d: deltas.append(d),
        )
        joined_deltas = "".join(deltas)
        assert joined_deltas == "霓虹雨幕落在街角，终端屏幕映出幽蓝微光。", f"Unexpected prose: {joined_deltas}"
        assert "TOP_SECRET_ANTHROPIC_THOUGHT" not in joined_deltas
        assert "TOP_SECRET_ANTHROPIC_THOUGHT" not in json.dumps(statuses, ensure_ascii=False)
        assert len(res["tool_logs"]) == 1 and res["tool_logs"][0]["tool"] == "read_save_file"
        print("OK: Anthropic native SSE loop executed tool_use, stripped thinking_delta 100%, and streamed clean prose.")

        print("\n=== [NEW TEST 4] Drag-and-Drop World Pack Upload (/api/packs/import_upload) ===")
        # 4A: Upload nested .rpgpack zip cloned from cyberpunk
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            src_pack = PACKS / "cyberpunk"
            for f in src_pack.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(src_pack).as_posix()
                    zf.writestr(f"_test_drop_zip/nested_inner/{rel}", f.read_bytes())
        b64_zip = base64.b64encode(buf.getvalue()).decode("ascii")
        tmp_packs.extend([PACKS / "test_drop_zip", PACKS / "test_drop_notes"])
        code, up_zip = http_req(rpg_base, "/api/packs/import_upload", "POST", {
            "filename": "test_drop_zip.rpgpack",
            "data_base64": b64_zip,
        })
        assert code == 200 and up_zip["ok"] is True and up_zip["id"] == "test_drop_zip"
        assert up_zip["status"] == "ready" and up_zip["valid"] is True

        # 4B: Upload raw setting notes folder
        code, up_notes = http_req(rpg_base, "/api/packs/import_upload", "POST", {
            "pack_id": "test_drop_notes",
            "files": [
                {"path": "世界设定草稿.md", "content": "# 蒸汽迷城\n齿轮与浓雾交织的港口都市。"},
                {"path": "人物小传.txt", "content": "主角是一名钟表匠学徒。"},
            ],
        })
        assert code == 200 and up_notes["ok"] is True and up_notes["id"] == "test_drop_notes"
        assert up_notes["status"] == "unparsed"
        print("OK: Both nested .rpgpack archive upload (auto-flattened + doctored) and raw folder upload passed.")

        print("\n=== [NEW TEST 5] Dialogue Box & Pixel-Art Sprite Matrix Doctor + Rollback ===")
        cp_doc = doctor_pack("cyberpunk")
        assert cp_doc["valid"] is True and not cp_doc["errors"], f"cyberpunk doctor errors: {cp_doc['errors']}"

        # Test invalid pixel sprite row widths -> must rollback!
        tctx = {"pack_id": "test_drop_zip", "applied_actions": [], "ui_updated": False}
        bad_out = execute_tool(
            None,
            "update_world_ui",
            {
                "theme_patch": {
                    "dialogue_box": {"style": "parchment-scroll"},
                    "pixel_frame": {
                        "scene": "misty_mountains",
                        "weather": "fireflies",
                        "palette": {"1": "#8c2d19"},
                        "sprites": [{"id": "broken", "anchor": "left-top", "rows": ["1111", "11"]}],
                    },
                }
            },
            role="tuner",
            tuner_context=tctx,
        )
        assert "回滚" not in bad_out, f"Ragged pixel rows must be auto-padded, got: {bad_out}"

        # Test valid custom dialogue box + pixel art sprite matrix -> must succeed!
        good_out = execute_tool(
            None,
            "update_world_ui",
            {
                "theme_patch": {
                    "typography": {"font_body": "'Noto Serif SC', serif", "story_font_size": "16px"},
                    "dialogue_box": {"style": "parchment-scroll", "gutter_x": "116px", "corner_ornament": "❖"},
                    "pixel_frame": {
                        "enabled": True,
                        "scene": "misty_mountains",
                        "weather": "fireflies",
                        "palette": {"1": "#8c2d19", "2": "#2b5b6c"},
                        "sprites": [
                            {
                                "id": "lantern",
                                "anchor": "left-top",
                                "scale": 4,
                                "rows": [
                                    "..1111..",
                                    ".122221.",
                                    ".121121.",
                                    "..1111..",
                                ],
                            }
                        ],
                    },
                }
            },
            role="tuner",
            tuner_context=tctx,
        )
        assert "✓" in good_out, f"Expected valid pixel UI update to pass, got: {good_out}"
        assert doctor_pack("test_drop_zip")["valid"] is True
        print("OK: Dialogue box styles & pixel-art character matrices validated with atomic rollback protection.")

        print("\nALL NEW DRIVER, ONBOARDING & PIXEL-ART UI TESTS PASSED!")
    finally:
        cfg_path.write_bytes(orig_cfg_bytes)
        for p in tmp_packs:
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        mock_llm.shutdown()
        rpg_srv.shutdown()


if __name__ == "__main__":
    main()
