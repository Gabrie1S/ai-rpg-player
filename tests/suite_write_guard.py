"""写回链路与失败可见性的回归测试：工具真的能写、引擎出错时熔断、写回失败不算完成、变更块写坏时能救则救。

覆盖 API、Harness 共用的工具执行沙箱，以及命令行驱动下的回写记录与推演进度约定。只应由 tests/run_all.py 在临时副本中运行。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import apply as apply_tool  # noqa: E402
import common  # noqa: E402
import context as ctxm  # noqa: E402
import turn_protocol as tp  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad  # noqa: E402
from engine.runtime import tool_sandbox as ts  # noqa: E402

SAVE, PACK = "_suite_write_guard", "xianxia"
Orch = om.RPGOrchestrator
API_DRIVER = {"type": "api", "api_format": "openai", "api_base": "https://api.deepseek.com/v1", "api_key": "sk-test", "model": "deepseek-chat"}
results = []


def case(name):
    def deco(fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:
            results.append((False, name, f"{type(exc).__name__}: {str(exc)[:700]}"))
        return fn
    return deco


def fresh():
    sd = ROOT / "saves" / SAVE
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", PACK], capture_output=True, cwd=ROOT, check=True)
    return sd


def now_meta(sd):
    return common.read_frontmatter(sd / "world/now.md")[0] or {}


def tool_call(name, args, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def reply_tools(*calls):
    return {"choices": [{"message": {"content": "", "tool_calls": list(calls)}, "finish_reason": "tool_calls"}]}


def reply_text(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class FakeApi:
    """替换 OpenAI 兼容接口的 HTTP 请求，按顺序返回写好的响应；工具照常走真实的执行沙箱。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.payloads = []

    def __enter__(self):
        self.saved = ad._post_json

        def fake_post(url, headers, payload, timeout=150.0):
            self.payloads.append(payload)
            return self.replies.pop(0)

        ad._post_json = fake_post
        return self

    def __exit__(self, *_):
        ad._post_json = self.saved


BAD_REPLACE = {"op": "replace", "file": "world/now.md", "old": "这段文字根本不存在", "new": "x"}
GOOD_SET = {"op": "set", "file": "world/now.md", "key": "ui_mood", "value": "rest"}


# ---------------- 问题 1：工具接口 ----------------
@case("apply_changes 工具真的能写：普通改动成功，锁定文件要 force_locked 才能改")
def _():
    sd = fresh()
    guard = ts.ToolRunGuard()
    out = ts.execute_tool(sd, "apply_changes", {"ops": [GOOD_SET]}, role="back", guard=guard)
    assert "已成功回写 1/1" in out, out
    assert now_meta(sd).get("ui_mood") == "rest"
    assert guard.writes_ok == 1 and not guard.unresolved_write_errors

    sheet = sd / "player/sheet.md"
    meta, body = common.read_frontmatter(sheet)
    meta["locked"] = True
    common.write_frontmatter(sheet, meta, body)
    op = {"op": "set", "file": "player/sheet.md", "key": "test_flag", "value": 1}
    out = ts.execute_tool(sd, "apply_changes", {"ops": [op]}, role="front", guard=guard)
    assert "回写失败" in out and "锁定" in out, out
    assert guard.unresolved_write_errors, "失败的写回要记下来"
    out = ts.execute_tool(sd, "apply_changes", {"ops": [op], "force_locked": True}, role="front", guard=guard)
    assert "已成功回写 1/1" in out, out
    assert common.read_frontmatter(sheet)[0].get("test_flag") == 1
    assert not guard.unresolved_write_errors, "之后写成功了，就不再算有未落地的改动"


@case("apply_operations：force_locked 参数等同于每条操作都带 force_locked")
def _():
    sd = fresh()
    sheet = sd / "player/sheet.md"
    meta, body = common.read_frontmatter(sheet)
    meta["locked"] = True
    common.write_frontmatter(sheet, meta, body)
    op = {"op": "set", "file": "player/sheet.md", "key": "test_flag", "value": 2}
    assert apply_tool.apply_operations(sd, [op], commit_checkpoint=False)["rolled_back"]
    res = apply_tool.apply_operations(sd, [op], commit_checkpoint=False, force_locked=True)
    assert res["ok"] and common.read_frontmatter(sheet)[0].get("test_flag") == 2


# ---------------- 问题 2：失败要有声音 ----------------
@case("熔断：同一个引擎异常第一次告诉模型别换写法，第二次直接中止")
def _():
    sd = fresh()
    guard = ts.ToolRunGuard()
    saved = ts.apply_operations

    def broken(*_a, **_k):
        raise TypeError("apply_operations() got an unexpected keyword argument 'x'")

    ts.apply_operations = broken
    try:
        first = ts.execute_tool(sd, "apply_changes", {"ops": [GOOD_SET]}, role="back", guard=guard)
        assert "引擎执行时出的错" in first and "中止" in first, first
        try:
            ts.execute_tool(sd, "apply_changes", {"ops": [GOOD_SET]}, role="back", guard=guard)
            raise AssertionError("第二次同样的异常应当熔断")
        except ts.EngineToolFault as fault:
            assert "TypeError" in str(fault)
    finally:
        ts.apply_operations = saved
    assert guard.unresolved_write_errors and guard.writes_failed == 2
    # 没有 guard 的老调用方式照旧只返回报错文字
    ts.apply_operations = broken
    try:
        assert "工具执行异常" in ts.execute_tool(sd, "apply_changes", {"ops": [GOOD_SET]}, role="back")
    finally:
        ts.apply_operations = saved


@case("熔断在 API 工具循环里生效：不再让模型反复重试同一个引擎错误")
def _():
    sd = fresh()
    saved = ts.apply_operations
    ts.apply_operations = lambda *_a, **_k: (_ for _ in ()).throw(TypeError("坏了"))
    try:
        with FakeApi(
            reply_tools(tool_call("apply_changes", {"ops": [GOOD_SET]}, "c1")),
            reply_tools(tool_call("apply_changes", {"ops": [GOOD_SET]}, "c2")),
            reply_text("不该走到这里"),
        ) as api:
            try:
                ad.run_openai_compat_loop(API_DRIVER, "系统", [{"role": "user", "content": "推演"}], sd, role="back")
                raise AssertionError("应当熔断")
            except ts.EngineToolFault:
                pass
        assert len(api.payloads) == 2 and api.replies, "第二次报错后就停，不再请求模型"
    finally:
        ts.apply_operations = saved


@case("幕后推演：写回失败且之后没写成，不算完成；改好重交成功则照常返回简报")
def _():
    sd = fresh()
    cfg = {**om.load_config(), "front_driver": {**API_DRIVER}, "back_driver": {"inherit_front": True}}
    with FakeApi(
        reply_tools(tool_call("apply_changes", {"ops": [BAD_REPLACE]}, "c1")),
        reply_text("简报：一切顺利"),
    ):
        try:
            Orch().run_world_sim(sd, {"task_type": "推演", "request": "推演"}, cfg)
            raise AssertionError("写回失败的推演不能当成完成")
        except om.WorldSimWriteFailed as exc:
            assert "没有写进存档" in str(exc)
    with FakeApi(
        reply_tools(tool_call("apply_changes", {"ops": [BAD_REPLACE]}, "c1")),
        reply_tools(tool_call("apply_changes", {"ops": [GOOD_SET]}, "c2")),
        reply_text("简报：一切顺利"),
    ):
        brief = Orch().run_world_sim(sd, {"task_type": "推演", "request": "推演"}, cfg)
    assert brief == "简报：一切顺利" and now_meta(sd).get("ui_mood") == "rest"


@case("引擎安排的推演：模型调 mark_ticked 不推进进度，进度由引擎验收后记录")
def _():
    sd = fresh()
    meta, body = common.read_frontmatter(sd / "world/now.md")
    meta["day"], meta["last_tick_day"] = 9, 1
    common.write_frontmatter(sd / "world/now.md", meta, body)
    cfg = {**om.load_config(), "front_driver": {**API_DRIVER}, "back_driver": {"inherit_front": True}}
    with FakeApi(reply_tools(tool_call("mark_ticked", {}, "c1")), reply_text("简报")) as api:
        Orch().run_world_sim(sd, {"task_type": "推演", "request": "推演"}, cfg, settle_by_engine=True)
    assert now_meta(sd).get("last_tick_day") == 1
    tool_reply = [m for m in api.payloads[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "引擎在验收通过后自动记录" in tool_reply
    with FakeApi(reply_tools(tool_call("mark_ticked", {}, "c1")), reply_text("简报")):
        Orch().run_world_sim(sd, {"task_type": "推演", "request": "推演"}, cfg)
    assert now_meta(sd).get("last_tick_day") == 9, "玩家或台前主动叫的推演，mark_ticked 照旧生效"


@case("回合前推演失败：存档回到推演前、进度不推进，并告诉玩家这次没结算")
def _():
    sd = fresh()
    meta, body = common.read_frontmatter(sd / "world/now.md")
    meta["day"], meta["last_tick_day"] = 9, 1
    common.write_frontmatter(sd / "world/now.md", meta, body)
    subprocess.run(["git", "-C", str(sd), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(sd), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "day9"], capture_output=True)
    orch = Orch()

    def failing_sim(sd_, args, cfg, on_status=None, settle_by_engine=False):
        assert settle_by_engine, "回合前推演由引擎记录进度"
        (sd_ / "world/director.md").write_text("半截改动", encoding="utf-8")
        raise om.WorldSimWriteFailed("幕后的改动没有写进存档：测试")

    orch.run_world_sim = failing_sim
    notices = []
    saved_plan = om.plot.needs_planning
    om.plot.needs_planning = lambda _sd: False
    try:
        brief = orch._run_due_sim(sd, om.load_config(), None, pre_head=om.timeline.head(sd), notices=notices)
    finally:
        om.plot.needs_planning = saved_plan
    assert brief == "" and now_meta(sd).get("last_tick_day") == 1
    assert (sd / "world/director.md").read_text(encoding="utf-8") != "半截改动", "半截改动要回滚"
    assert notices and "没有完成" in notices[0], notices


@case("/gm 改存档失败：回复后面附上引擎提示，结果消息里也说明没写进去")
def _():
    sd = fresh()
    cfg = dict(om.load_config())
    cfg["front_driver"] = {**API_DRIVER}
    cfg["back_driver"] = {"inherit_front": True}
    saved_cfg = om.load_config
    om.load_config = lambda: cfg
    try:
        with FakeApi(
            reply_tools(tool_call("apply_changes", {"ops": [BAD_REPLACE]}, "c1")),
            reply_text("已经按你的要求改好了。"),
        ):
            res = Orch()._run_turn_core("/gm 把爹写得温和一点", save_name=SAVE)
    finally:
        om.load_config = saved_cfg
    assert res["command"] == "gm" and not res["archived"]
    assert "已经按你的要求改好了。" in res["story"] and "引擎提示" in res["story"], res["story"]
    assert "没有写进存档" in res["message"], res["message"]


@case("命令行驱动：apply.py 把回写成败记进引擎给的记录文件，mark-ticked 按约定跳过")
def _():
    sd = fresh()
    fd, ledger = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    env = {**os.environ, apply_tool.WRITE_LEDGER_ENV: ledger, "RPG_SAVE": SAVE}
    try:
        bad = subprocess.run([sys.executable, "engine/tools/apply.py", "--no-commit", "--json", json.dumps([BAD_REPLACE], ensure_ascii=False)],
                             cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        good = subprocess.run([sys.executable, "engine/tools/apply.py", "--no-commit", "--json", json.dumps([GOOD_SET])],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        assert bad.returncode == 1 and good.returncode == 0, (bad.stdout, good.stdout)
        entries = apply_tool.read_write_ledger(ledger)
        assert [ok for ok, _ in entries] == [False, True] and entries[0][1], entries
    finally:
        Path(ledger).unlink(missing_ok=True)

    meta, body = common.read_frontmatter(sd / "world/now.md")
    meta["day"], meta["last_tick_day"] = 5, 1
    common.write_frontmatter(sd / "world/now.md", meta, body)
    import clock
    out = subprocess.run([sys.executable, "engine/tools/clock.py", "mark-ticked"], cwd=ROOT,
                         env={**os.environ, "RPG_SAVE": SAVE, clock.ENGINE_SETTLES_TICK_ENV: "1"},
                         capture_output=True, text=True, encoding="utf-8")
    assert "不需要 mark-ticked" in out.stdout and now_meta(sd).get("last_tick_day") == 1, out.stdout + out.stderr


@case("命令行驱动的幕后推演：apply.py 记下的失败同样让推演判为没完成")
def _():
    sd = fresh()
    cfg = {**om.load_config(), "back_driver": {"inherit_front": False, "type": "cli", "cli_tool": "fakecli", "cli_args": []}}
    seen_env = {}
    saved = om.run_cli_turn

    def fake_cli(driver, prompt, **kw):
        seen_env.update(driver.get("engine_env") or {})
        with open(seen_env[apply_tool.WRITE_LEDGER_ENV], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ok": False, "errors": ["第1条：要替换的文字出现了 0 次"]}, ensure_ascii=False) + "\n")
        return {"text": "简报", "tool_logs": [], "model": "fake"}

    om.run_cli_turn = fake_cli
    try:
        try:
            Orch().run_world_sim(sd, {"task_type": "推演", "request": "推演"}, cfg, settle_by_engine=True)
            raise AssertionError("命令行回写失败也不能当成完成")
        except om.WorldSimWriteFailed:
            pass
    finally:
        om.run_cli_turn = saved
    assert seen_env.get("RPG_ENGINE_SETTLES_TICK") == "1"
    assert not Path(seen_env[apply_tool.WRITE_LEDGER_ENV]).exists(), "记录文件用完要删"


# ---------------- 问题 6：变更块 ----------------
@case("变更块写坏时能确定原意的救回来：提前收尾、两段相连、末尾多括号或逗号")
def _():
    a, b = '{"op":"set","file":"world/now.md","key":"a","value":1}', '{"op":"set","file":"world/now.md","key":"b","value":2}'
    for body in (f"[{a}]}},{b}]", f"[{a}],{b}]", f"[{a}]\n[{b}]", f"[{a},{b}]]", f"[{a},{b},]", f"[{a}]\n{b}"):
        text, ops, err = tp.split_changes(f"正文\n<state_changes>\n{body}\n</state_changes>")
        assert text == "正文" and not err and [op["key"] for op in ops] == ["a", "b"], (body, ops, err)
    # 拿不准的不救：中间半截、末尾跟着文字（多半被截断或混进了说明）
    for body in (f'[{a},{{"op":"set","fi', f"[{a}] 以上就是全部改动", "[{坏的"):
        text, ops, err = tp.split_changes(f"正文\n<state_changes>\n{body}\n</state_changes>")
        assert text == "正文" and not ops and "JSON" in err, (body, ops, err)


@case("变更块漏了：第一次补问落空（接口报错或只想不写），再问一次补上")
def _():
    sd = fresh()
    ops = json.dumps([GOOD_SET], ensure_ascii=False)
    calls = []
    replies = [
        "你推门进去。",
        RuntimeError("模型没有返回最终回复"),
        f"<state_changes>\n{ops}\n</state_changes>",
    ]
    cfg = dict(om.load_config())
    cfg["front_driver"] = {**API_DRIVER}
    cfg["back_driver"] = {"inherit_front": True}
    saved = (om.run_openai_compat_loop, om.load_config, om.plot.needs_planning)

    def fake(driver, system_prompt, messages, sd_, role="front", **kw):
        calls.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"text": reply, "tool_logs": [], "model": "fake"}

    om.run_openai_compat_loop, om.load_config, om.plot.needs_planning = fake, (lambda: cfg), (lambda _sd: False)
    try:
        res = Orch()._run_turn_core("推门", save_name=SAVE)
    finally:
        om.run_openai_compat_loop, om.load_config, om.plot.needs_planning = saved
    assert len(calls) == 3 and res["state_synced"], (len(calls), res["message"])
    assert now_meta(sd).get("ui_mood") == "rest"
    assert not ctxm.read_pending_turn(sd).get("missing_changes")


sd_final = ROOT / "saves" / SAVE
if sd_final.exists():
    shutil.rmtree(sd_final, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
