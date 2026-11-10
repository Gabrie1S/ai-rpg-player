"""End-to-end verification of SSE Typewriter Streaming, Anti-Spoiler Filtering, World-Sim Live Status, and AI JSONL Telemetry."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


def _rmtree_win(path):
    def _onexc(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if Path(path).exists():
        shutil.rmtree(path, onexc=_onexc)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine" / "tools"))


class MockLLMHandler(BaseHTTPRequestHandler):
    """Simulates an OpenAI-compatible SSE upstream with reasoning_content, <think> splits, pre-tool babble, and nested world-sim tool calls."""
    call_seq = 0

    def log_message(self, fmt, *args):
        pass

    def _sse(self, obj):
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        # Verify Authorization header was sent, and later verify it is REDACTED in logs!
        auth = self.headers.get("Authorization", "")
        assert "sk-testsecretkey999999" in auth

        MockLLMHandler.call_seq += 1
        seq = MockLLMHandler.call_seq
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()

        if seq == 1:
            # Front GM Step 1: emits secret reasoning_content + short pre-tool babble + call_world_sim tool call
            self._sse({"choices": [{"delta": {"reasoning_content": "【绝密思考】幕后真凶其实是泰坦副总，先调world-sim推演伏击"}}]})
            self._sse({"choices": [{"delta": {"content": "我先查一下秘密档案。"}}]})
            self._sse({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_ws_1",
                            "type": "function",
                            "function": {
                                "name": "call_world_sim",
                                "arguments": json.dumps({
                                    "task_type": "推演",
                                    "day_range": "第1天",
                                    "request": "检查 world/secrets/阴谋总纲.md 并掷骰判定暗流",
                                }, ensure_ascii=False),
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }]
            })
        elif seq == 2:
            # Back world-sim Step 1: emits <think> + calls roll_dice
            self._sse({"choices": [{"delta": {"content": "<think>正在读取 world/secrets/阴谋总纲.md 准备暗中检定</think>"}}]})
            self._sse({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_dice_1",
                            "type": "function",
                            "function": {
                                "name": "roll_dice",
                                "arguments": json.dumps({"mod": 1, "dc": "困难", "note": "黑石帮秘密潜入检定"}, ensure_ascii=False),
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }]
            })
        elif seq == 3:
            # Back world-sim Step 2: returns internal brief (must NOT stream as prose to player!)
            self._sse({"choices": [{"delta": {"content": "【幕后内部简报】码头巡逻已加密。"}, "finish_reason": "stop"}]})
        else:
            # Front GM Step 2: fragmented <think> across chunks + multi-chunk visible story prose!
            chunks = [
                "\n<thi",
                "nk>根据幕后简报描写表面景象，绝不剧透泰坦副总与潜入者身份</th",
                "ink>\n\n",
                "冷雨顺着生锈的龙门吊滑落，将码头的霓虹倒影砸得粉碎。",
                "远处河道上掠过两道惨白的探照灯光束，低沉的涡轮轰鸣贴着水面压了过来。",
                "\n\n“今晚巡逻艇比平时多了两倍，”老霍克压低了帽檐，",
                "不动声色地把扳手塞进油污斑驳的风衣内袋。",
            ]
            for i, ch in enumerate(chunks):
                self._sse({
                    "choices": [{
                        "delta": {"content": ch},
                        "finish_reason": "stop" if i == len(chunks) - 1 else None,
                    }]
                })

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def run_all():
    from ai_log import ENGINE_LOG_PATH, ERRORS_LOG_PATH, query_logs, sanitize_payload
    from common import SAVES, load_config, save_config
    from engine.runtime.api_driver import StreamThinkStripper, execute_tool
    from newsave import create_save, set_current
    from web.server import StdlibRPGHandler

    print("=== [STREAM TEST 1] StreamThinkStripper Edge Cases ===")
    events = []
    st = StreamThinkStripper(on_think_start=lambda: events.append("THINK"))
    pieces = ["  \n", "<th", "inking>secret plot 123</think", "ing>\n\nHello ", "World!", "<think>unclosed secret"]
    vis = "".join(st.feed(p) for p in pieces) + st.finish()
    assert vis == "Hello World!", f"Unexpected visible output: {vis!r}"
    assert "secret plot 123" in st.thinking_text and "unclosed secret" in st.thinking_text
    assert events == ["THINK"]
    print("OK: StreamThinkStripper cleanly stripped split & unclosed <think> tags.")

    print("\n=== [STREAM TEST 2] Front GM And Back Role Can Read Secrets ===")
    orig_save = (SAVES / "current.txt").read_text(encoding="utf-8").strip() if (SAVES / "current.txt").exists() else "霓虹测试"
    tmp_save_name = "_test_stream_save"
    tmp_sd = SAVES / tmp_save_name
    _rmtree_win(tmp_sd)

    orig_cfg = load_config()
    mock_llm = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    llm_port = mock_llm.server_address[1]
    threading.Thread(target=mock_llm.serve_forever, daemon=True).start()

    rpg_srv = ThreadingHTTPServer(("127.0.0.1", 0), StdlibRPGHandler)
    rpg_port = rpg_srv.server_address[1]
    threading.Thread(target=rpg_srv.serve_forever, daemon=True).start()

    try:
        sd = create_save(tmp_save_name, "cyberpunk")
        (sd / "world" / "secrets").mkdir(parents=True, exist_ok=True)
        (sd / "world" / "secrets" / "阴谋总纲.md").write_text("终极机密", encoding="utf-8")

        acl_res = execute_tool(sd, "read_save_file", {"rel_path": "world/secrets/阴谋总纲.md"}, role="front")
        assert acl_res == "终极机密", f"Front GM must be able to read secrets, got: {acl_res}"
        back_res = execute_tool(sd, "read_save_file", {"rel_path": "world/secrets/阴谋总纲.md"}, role="back")
        assert back_res == "终极机密", f"Expected back role allowed, got: {back_res}"
        print("OK: Front GM and back world-sim both read world/secrets/.")

        print("\n=== [STREAM TEST 3] Live End-to-End HTTP SSE Streaming + Anti-Spoiler Guarantee ===")
        test_cfg = json.loads(json.dumps(orig_cfg))
        test_cfg["front_driver"] = {
            "type": "api",
            "api_base": f"http://127.0.0.1:{llm_port}/v1",
            "api_key": "sk-testsecretkey999999",
            "model": "mock-reasoner-v1",
        }
        test_cfg["back_driver"] = {"inherit_front": True}
        save_config(test_cfg)

        req = urllib.request.Request(
            f"http://127.0.0.1:{rpg_port}/api/turn/stream",
            data=json.dumps({"input": "观察码头动静（场外：压迫感强一点）", "save_name": tmp_save_name}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sse_events = []
        raw_wire = []
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            assert "text/event-stream" in (resp.headers.get("Content-Type") or "")
            for raw_ln in resp:
                ln = raw_ln.decode("utf-8").strip()
                if ln.startswith("data:"):
                    raw_wire.append(ln)
                    sse_events.append(json.loads(ln[5:].strip()))

        full_wire_str = "\n".join(raw_wire)
        # CRITICAL ANTI-SPOILER ASSERTIONS: NONE of these internal strings may appear ANYWHERE in the SSE wire stream!
        forbidden_leaks = [
            "泰坦副总",
            "绝密思考",
            "我先查一下秘密档案",
            "阴谋总纲",
            "world/secrets",
            "黑石帮秘密潜入检定",
            "幕后内部简报",
            "<think",
            "</think",
            "tool_logs",
            "sk-testsecretkey999999",
        ]
        for leak in forbidden_leaks:
            assert leak not in full_wire_str, f"SPOILER / SECRET LEAKED ON SSE WIRE: {leak!r}"

        statuses = [e for e in sse_events if e.get("type") == "status"]
        deltas = [e["text"] for e in sse_events if e.get("type") == "delta"]
        dones = [e for e in sse_events if e.get("type") == "done"]

        assert len(dones) == 1, f"Expected 1 done event, got {dones}"
        assert any(s.get("world_sim") is True and "幕后" in s.get("label", "") for s in statuses), (
            f"Expected spoiler-free world-sim status indicator, got: {statuses}"
        )
        assert len(deltas) >= 2, f"Expected incremental prose deltas, got: {deltas}"
        streamed_prose = "".join(deltas)
        expected_prose = (
            "冷雨顺着生锈的龙门吊滑落，将码头的霓虹倒影砸得粉碎。"
            "远处河道上掠过两道惨白的探照灯光束，低沉的涡轮轰鸣贴着水面压了过来。"
            "\n\n“今晚巡逻艇比平时多了两倍，”老霍克压低了帽檐，"
            "不动声色地把扳手塞进油污斑驳的风衣内袋。"
        )
        assert streamed_prose == expected_prose, f"Mismatch:\nGot: {streamed_prose!r}\nExp: {expected_prose!r}"
        print(f"OK: Received {len(statuses)} spoiler-free status transitions and {len(deltas)} clean typewriter deltas; 0 leaks on wire!")

        print("\n=== [STREAM TEST 4] AI JSONL Diagnostic Logs & Cross-Process Trace Correlation ===")
        tid = dones[0]["trace_id"]
        assert tid.startswith("turn_"), f"Unexpected trace_id: {tid}"
        trace_lines = query_logs(trace_id=tid)
        assert len(trace_lines) >= 8, f"Expected rich trace events for {tid}, got {len(trace_lines)}"
        trace_objs = [json.loads(x) for x in trace_lines]
        ev_names = [o["ev"] for o in trace_objs]
        for expected_ev in (
            "turn.begin", "turn.anchor", "turn.context", "llm.req", "llm.think", "llm.resp",
            "world_sim.invoke", "dice.check", "turn.archived", "turn.state_sync", "turn.outcome", "turn.end",
        ):
            assert expected_ev in ev_names, f"Missing event {expected_ev!r} in trace {ev_names}"
        outcome = next(o["data"] for o in trace_objs if o["ev"] == "turn.outcome")
        assert outcome["archived"] and outcome["snapshot_saved"] and outcome["head_after"] != outcome["head_before"]
        assert outcome["state_synced"] == dones[0]["result"]["state_synced"]
        request = next(o["data"] for o in trace_objs if o["ev"] == "llm.req")
        assert request["message_chars"] > 0 and request["tool_reply_chars"] == 0

        import ai_log

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "engine.jsonl"
            for i, path in enumerate((target.with_suffix(".3.jsonl"), target.with_suffix(".2.jsonl"), target)):
                path.write_text(json.dumps({"tid": tid, "ctx": {"save": tmp_sd.name},
                                            "ev": "turn.outcome", "seq": i}) + "\n", encoding="utf-8")
            with patch.object(ai_log, "ENGINE_LOG_PATH", target):
                assert len(query_logs(trace_id=tid)) == 3
                tail = query_logs(save_name=tmp_sd.name, ev_prefix="turn.outcome", limit=2)
                assert [json.loads(line)["seq"] for line in tail] == [1, 2]
                assert not query_logs(save_name="unrelated")

        # Notice `dice.check` ran inside a SEPARATE Python subprocess (`dice.py`) and still inherited `tid` via RPG_TRACE_ID!
        # Also verify reasoning IS stored in AI log for debugging while API key is redacted!
        joined_log = "\n".join(trace_lines)
        assert "泰坦副总" in joined_log, "Expected full reasoning preserved inside AI diagnostic log!"
        assert "sk-testsecretkey999999" not in ENGINE_LOG_PATH.read_text(encoding="utf-8"), "Raw API key must NEVER appear in logs!"
        scrubbed = sanitize_payload({"api_key": "sk-testsecretkey999999", "hdr": "Bearer sk-testsecretkey999999"})
        assert "999999" not in json.dumps(scrubbed)

        # Test CLI query tool `python engine/tools/ai_log.py --stats`
        cli_r = subprocess.run(
            [sys.executable, str(ROOT / "engine" / "tools" / "ai_log.py"), "--stats"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        stats_obj = json.loads(cli_r.stdout)
        assert stats_obj["engine_bytes"] > 0 and ERRORS_LOG_PATH.exists()
        print(f"OK: Trace {tid} correlated {len(trace_objs)} cross-process JSONL events (including child dice.py) with 100% key redaction!")

    finally:
        save_config(orig_cfg)
        set_current(orig_save)
        _rmtree_win(tmp_sd)
        mock_llm.shutdown()
        mock_llm.server_close()
        rpg_srv.shutdown()
        rpg_srv.server_close()


if __name__ == "__main__":
    run_all()
    print("\nALL STREAMING, ANTI-SPOILER & AI LOGGING TESTS PASSED!")
