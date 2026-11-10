"""通用化架构改造回归测试（覆盖九嶷实战踩坑修复与多驱动对等性）。只应由 tests/run_all.py 在临时副本中运行。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import apply as apply_tool  # noqa: E402
import common  # noqa: E402
import context as ctxm  # noqa: E402
import mods as mods_layer  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad, cli_driver as cd  # noqa: E402
from engine.runtime import driver_config as dc  # noqa: E402

SAVE = "_suite_univ"
Orch = om.RPGOrchestrator
results = []


def case(name):
    def deco(fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:
            results.append((False, name, f"{type(exc).__name__}: {str(exc)[:500]}"))
        return fn
    return deco


def fresh(pack="cyberpunk"):
    sd = ROOT / "saves" / SAVE
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", pack], capture_output=True, cwd=ROOT, check=True)
    return sd


def git_out(sd, *args):
    return subprocess.run(["git", "-C", str(sd), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


@case("apply.py：兼容字段别名、无 section 追加、YAML 缩进容错与日志截断行唯一替换")
def _():
    sd = fresh()
    # 1. 字段别名 + 缺省 section 追加
    chron = sd / "world/events/chronicle.md"
    res1 = apply_tool.apply_operations(sd, [
        {"op": "set", "path": "world/now.md", "field": "ui_mood", "value": "mystery"},
        {"op": "append", "rel_path": "world/events/chronicle.md", "content": "- [第1天] 测试缺省小节追加"},
    ], commit_checkpoint=False)
    assert res1["ok"] and not res1["rolled_back"], res1
    assert common.read_frontmatter(sd / "world/now.md")[0]["ui_mood"] == "mystery"
    assert "测试缺省小节追加" in chron.read_text(encoding="utf-8")

    # 2. timeline.yaml 4 空格缩进容错（复现九嶷幕后 5.2 分钟回滚根因）
    tl = sd / "world/events/timeline.yaml"
    tl.write_text(
        "events:\n"
        "  - id: evt_01\n"
        "    title: 码头交货\n"
        "    day: 5\n"
        "    status: planned\n",
        encoding="utf-8",
    )
    res2 = apply_tool.apply_operations(sd, [{
        "op": "replace",
        "file": "world/events/timeline.yaml",
        "target": "- id: evt_01\n  title: 码头交货\n  day: 5\n  status: planned",
        "replacement": "- id: evt_01\n  title: 码头交货\n  day: 5\n  status: happened",
    }], commit_checkpoint=False)
    assert res2["ok"] and not res2["rolled_back"], res2
    updated_tl = tl.read_text(encoding="utf-8")
    assert "  - id: evt_01\n    title: 码头交货\n    day: 5\n    status: happened" in updated_tl, updated_tl

    # 3. journal.md 被 context.py 截断加 `…` 的长行唯一替换，以及重复前缀拒绝替换
    long_tail = "细节记录" * 35
    jpath = sd / "player/journal.md"
    jpath.write_text(
        f"# 日志\n\n## 当前目标\n- 调查七号船坞账本：{long_tail}\n",
        encoding="utf-8",
    )
    ctx_j = ctxm.assemble_turn_context(sd, "看看账本")["files"]["player/journal.md"]
    trunc_line = [ln for ln in ctx_j.splitlines() if ln.startswith("- 调查七号船坞账本")][0]
    assert trunc_line.endswith("…")
    res3 = apply_tool.apply_operations(sd, [{
        "op": "replace",
        "file": "player/journal.md",
        "old": trunc_line,
        "new": "- 调查七号船坞账本：已查清走私买主",
    }], commit_checkpoint=False)
    assert res3["ok"] and "已查清走私买主" in jpath.read_text(encoding="utf-8"), res3

    # 出现两处同缩进匹配时必须严格拒绝，绝不盲改
    jpath.write_text("# 日志\n\n## 当前目标\n  - 同一待办\n  - 同一待办\n", encoding="utf-8")
    res4 = apply_tool.apply_operations(sd, [{
        "op": "replace",
        "file": "player/journal.md",
        "old": "- 同一待办",
        "new": "- 已完成",
    }], commit_checkpoint=False)
    assert res4["rolled_back"] and "必须恰好 1 次" in res4["errors"][0], res4


@case("world-sim：常规推演预装配核心幕后上下文，开局种子生成不预装空白上下文且放宽步数")
def _():
    sd = fresh()
    apply_tool.archive_story_turn(sd, "潜入船坞", "你在雨夜潜入了船坞仓库。")
    seen = []
    orig_loop, orig_load = om.run_openai_compat_loop, om.load_config
    cfg = orig_load()
    om.load_config = lambda: {**cfg, "front_driver": {"type": "api", "api_key": "sk-t", "model": "deepseek-chat"}, "back_driver": {"inherit_front": True}}

    def fake_loop(driver_cfg, sys_prompt, messages, save_sd, **kw):
        seen.append({"messages": messages, "max_steps": kw.get("max_steps")})
        return {"text": "简报", "model": "mock"}

    om.run_openai_compat_loop = fake_loop
    try:
        orch = Orch()
        orch.run_world_sim(sd, {"task_type": "推演", "day_range": "第1天~第7天", "request": "结算到期事件"}, om.load_config())
        orch.run_world_sim(sd, {"task_type": "开局种子生成", "day_range": "第0天", "request": "生成母本"}, om.load_config())
    finally:
        om.run_openai_compat_loop, om.load_config = orig_loop, orig_load

    assert seen[0]["max_steps"] == 30
    assert "当前存档核心资料" in seen[0]["messages"][0]["content"]
    assert "你在雨夜潜入了船坞仓库" in seen[0]["messages"][0]["content"]
    assert seen[1]["max_steps"] == 50
    assert "当前存档核心资料" not in seen[1]["messages"][0]["content"]


@case("事务原子性：回合前 world-sim 运行中途玩家取消，脏文件与待推演队列完整回滚")
def _():
    sd = fresh()
    orch = Orch()
    # 先写入一条造物请求并提交基线
    pending_p = ctxm.pending_turn_path(sd)
    pending_p.parent.mkdir(parents=True, exist_ok=True)
    pending_p.write_text(json.dumps({"sims": [{"task": "造物", "detail": "地下黑市"}]}, ensure_ascii=False), encoding="utf-8")

    orig_loop, orig_load = om.run_openai_compat_loop, om.load_config
    cfg = orig_load()
    om.load_config = lambda: {**cfg, "front_driver": {"type": "api", "api_key": "sk-t", "model": "deepseek-chat"}, "back_driver": {"inherit_front": True}}

    def cancelling_sim(*_a, **_kw):
        # 模拟幕后推演中途写了 dice.log 并改了 now.md，随后玩家点停止生成
        (sd / "log").mkdir(parents=True, exist_ok=True)
        (sd / "log/dice.log").write_text("dirty roll\n", encoding="utf-8")
        m, b = common.read_frontmatter(sd / "world/now.md")
        m["ui_mood"] = "combat"
        common.write_frontmatter(sd / "world/now.md", m, b)
        raise om.StreamCancelled("玩家停止")

    om.run_openai_compat_loop = cancelling_sim
    try:
        try:
            orch._run_turn_core("去地下黑市", save_name=SAVE)
            raise AssertionError("应当抛出 StreamCancelled")
        except om.StreamCancelled:
            pass
    finally:
        om.run_openai_compat_loop, om.load_config = orig_loop, orig_load

    assert not (sd / "log/dice.log").exists(), "被取消的幕后掷骰日志应当被回滚清除"
    assert common.read_frontmatter(sd / "world/now.md")[0].get("ui_mood") != "combat"
    assert json.loads(pending_p.read_text(encoding="utf-8"))["sims"][0]["detail"] == "地下黑市"
    assert not git_out(sd, "status", "--porcelain")


@case("API 调教助手：拥有完整文件分流指引、目录枚举、结构化 Schema 与 [tuner] 快照校验")
def _():
    sd = fresh()
    # 目录枚举能力（save: 与 pack:）
    pack_list = ad.execute_tool(sd, "read_tuner_target", {"target": "pack:"}, role="tuner", tuner_context={"pack_id": "cyberpunk"})
    save_list = ad.execute_tool(sd, "read_tuner_target", {"target": "save:world"}, role="tuner", tuner_context={"pack_id": "cyberpunk"})
    assert "pack.yaml" in pack_list and "now.md" in save_list, (pack_list, save_list)

    # 结构化 OPS_ITEM_SCHEMA 注入检查
    apply_props = ad.FRONT_TOOLS_OPENAI[2]["function"]["parameters"]["properties"]["ops"]["items"]["properties"]
    tuner_props = ad.TUNER_TOOLS_OPENAI[3]["function"]["parameters"]["properties"]["save_ops"]["items"]["properties"]
    assert {"op", "file", "old", "new", "section", "text"}.issubset(apply_props.keys())
    assert {"op", "file", "old", "new", "section", "text"}.issubset(tuner_props.keys())

    # API 模式下运行调教助手：验证提示词分流指引与 [tuner] 快照提交
    captured = {}
    orig_loop, orig_load = om.run_openai_compat_loop, om.load_config
    cfg = orig_load()
    om.load_config = lambda: {**cfg, "front_driver": {"type": "api", "api_key": "sk-t", "model": "deepseek-chat"}}

    def fake_tuner_loop(driver_cfg, sys_prompt, messages, save_sd, **kw):
        captured["sys"] = sys_prompt
        captured["user"] = messages[-1]["content"]
        mods_layer.write_files(save_sd, {"style_pack.md": "- 台词干脆\n"}, label="调文风")
        return {"text": "已调整本局文风。", "model": "mock"}

    om.run_openai_compat_loop = fake_tuner_loop
    try:
        res = Orch()._run_tuner_core("台词再干脆一点", save_name=SAVE)
    finally:
        om.run_openai_compat_loop, om.load_config = orig_loop, orig_load

    assert res["ok"]
    combined = captured["sys"] + "\n" + captured["user"]
    for kw_need in ("player/persona.md", "world/npcs/<id>.md", "world/director.md", "update_world_or_save", "不放 mods", "这里没有命令行"):
        assert kw_need in combined, kw_need
    assert captured["user"].rstrip().endswith("读到了也不要写进回复。")
    assert git_out(sd, "log", "-1", "--format=%s").startswith("[tuner]")


@case("中立命令与确定性斜杠指令：/undo 纯撤回、/export 导出、/snapshot 封存与 /gm 工具")
def _():
    sd = fresh()
    orch = Orch()
    apply_tool.archive_story_turn(sd, "第一轮行动", "第一轮正文。")
    apply_tool.git_turn_checkpoint(sd, "第一轮行动", prefix="[turn]")
    apply_tool.archive_story_turn(sd, "第二轮行动", "第二轮正文。")
    apply_tool.git_turn_checkpoint(sd, "第二轮行动", prefix="[turn]")

    # 1. /undo 只撤回不重演（区别于 /retry）
    u_res = orch._run_turn_core("/undo", save_name=SAVE)
    assert u_res["command"] == "undo" and u_res.get("reverted_only") is True
    turns = om.parse_story_History(sd)
    assert len(turns) == 1 and turns[0]["input"] == "第一轮行动"

    # 2. /export novel 确定性导出且不计入回合
    ex_res = orch._run_turn_core("/export novel", save_name=SAVE)
    assert ex_res["command"] == "export" and not ex_res["archived"] and "exports/" in ex_res["story"]
    assert len(om.parse_story_History(sd)) == 1

    # 3. /snapshot 在 API 模式下静默调用幕后推演并提交 [snapshot]
    orig_loop, orig_load = om.run_openai_compat_loop, om.load_config
    cfg = orig_load()
    om.load_config = lambda: {**cfg, "front_driver": {"type": "api", "api_key": "sk-t", "model": "deepseek-chat"}, "back_driver": {"inherit_front": True}}

    def fake_snap_loop(driver_cfg, sys_prompt, messages, save_sd, **kw):
        snap_file = save_sd / "log/snapshots/第1卷-第0天.md"
        snap_file.parent.mkdir(parents=True, exist_ok=True)
        snap_file.write_text("# 快照\n机密进展（仅落盘）", encoding="utf-8")
        return {"text": "快照已落盘", "model": "mock"}

    om.run_openai_compat_loop = fake_snap_loop
    try:
        sn_res = orch._run_turn_core("/snapshot", save_name=SAVE)
    finally:
        om.run_openai_compat_loop, om.load_config = orig_loop, orig_load

    assert sn_res["command"] == "snapshot" and "机密进展" not in sn_res["story"]
    assert (sd / "log/snapshots/第1卷-第0天.md").exists()
    assert git_out(sd, "log", "-1", "--format=%s").startswith("[snapshot]")

    # 4. API 下 manage_timeline 工具与 Codex CLI 沙箱参数
    log_txt = ad.execute_tool(sd, "manage_timeline", {"action": "log", "limit": 5}, role="front")
    assert "[snapshot]" in log_txt and "[turn]" in log_txt, log_txt
    cmd_ro, _, _ = cd._build_cli_invocation("codex.exe", "codex", "hi", [], tools="Read,Grep,Glob")
    cmd_rw, _, _ = cd._build_cli_invocation("codex.exe", "codex", "hi", [], tools=None)
    assert "--sandbox" in cmd_ro and "read-only" in cmd_ro
    assert "--full-auto" in cmd_rw


@case("本地密钥与验证态隔离：首次保存与已有覆盖均只写入 config.local.yaml")
def _():
    local_p = common.CONFIG_LOCAL_PATH
    cfg_p = common.CONFIG_PATH
    orig_cfg = cfg_p.read_bytes()
    try:
        local_p.unlink(missing_ok=True)
        loaded = common.load_config()
        loaded["front_driver"]["api_key"] = "sk-fresh-only-secret"
        loaded["back_driver"]["api_key"] = "sk-back-only-secret"
        common.save_config(loaded)
        assert "only-secret" not in cfg_p.read_text(encoding="utf-8")
        assert common.load_config()["back_driver"]["api_key"] == "sk-back-only-secret"
        changed = yaml.safe_load(cfg_p.read_text(encoding="utf-8"))
        changed["front_driver"]["api_base"] = "https://different.example/v1"
        cfg_p.write_text(yaml.safe_dump(changed), encoding="utf-8")
        assert common.load_config()["front_driver"]["api_key"] == ""
        cfg_p.write_bytes(orig_cfg)

        local_p.write_text(
            yaml.safe_dump({"front_driver": {"api_key": "sk-local-only-secret"}}, allow_unicode=True),
            encoding="utf-8",
        )
        loaded = common.load_config()
        assert loaded["front_driver"]["api_key"] == "sk-local-only-secret"
        loaded["front_driver"]["model"] = "deepseek-v4-pro"
        loaded["driver_verified"] = True
        loaded["driver_verified_at"] = "2026-11-08 12:00:00"
        common.save_config(loaded)
        tracked_raw = cfg_p.read_text(encoding="utf-8")
        local_raw = local_p.read_text(encoding="utf-8")
        assert "sk-local-only-secret" not in tracked_raw
        assert "driver_verified" not in tracked_raw
        assert "sk-local-only-secret" in local_raw and "driver_verified: true" in local_raw
    finally:
        if local_p.exists():
            local_p.unlink()
        cfg_p.write_bytes(orig_cfg)

@case("驱动：凭证限于对应端点，变更配置使旧验证失效")
def _():
    official = {"api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"}
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key", "OPENAI_API_KEY": "openai-test-key"}, clear=True):
        assert dc.resolve_api_key(official) == "deepseek-test-key"
        for base in ("https://other.example/v1", "https://api.deepseek.com.other.example/v1",
                     "https://api.deepseek.com@other.example/v1", "https://other.example/api.deepseek.com",
                     "http://api.deepseek.com/v1", "https://api.deepseek.com:444/v1"):
            assert not dc.resolve_api_key({"api_base": base, "api_format": "anthropic"})
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-test-key"}):
            assert dc.resolve_api_key({"type": "harness", "harness_mode": "openai_agents_sdk",
                                       "api_format": "anthropic"}) == "deepseek-test-key"
        cfg = {"front_driver": official, "driver_verified": True}
        cfg["driver_verified_for"] = dc.driver_fingerprint(cfg)
        assert dc.has_driver_ready(cfg)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "changed"}):
            assert not dc.has_driver_ready(cfg)

    existing = {**official, "api_key": "saved", "api_key_env": "DEEPSEEK_API_KEY"}
    assert dc.merge_driver_config(existing, {"model": "new", "api_key": ""})["api_key"] == "saved"
    new = dc.merge_driver_config(existing, {"api_base": "https://custom.example/v1", "api_key": ""})
    assert not new["api_key"] and not new["api_key_env"]
    assert dc.merge_driver_config(existing, {"api_base": new["api_base"], "api_key": "new"})["api_key"] == "new"


@case("存档：直接名称、环境变量和候选标记均不能指向存档目录之外")
def _():
    import world_candidates as candidates
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        saves = root / "saves"
        good = saves / "合法存档"
        (good / "world").mkdir(parents=True)
        (root / "outside").mkdir()
        (good / candidates.MARKER).write_text("replaces: ../outside\n", encoding="utf-8")
        with patch.object(common, "SAVES", saves), patch.object(candidates, "SAVES", saves):
            assert common.save_dir(good.name) == good
            for name in ("../outside", "..\\outside", str(root / "outside"), "C:outside", "合法存档."):
                assert common.save_dir(name, required=False) is None
                with patch.dict(os.environ, {"RPG_SAVE": name}):
                    assert common.save_dir(required=False) is None
            assert candidates.replaces_of(good) == ""


@case("验证：空响应、截断响应、未完成工具循环和只有版本号的 CLI 均失败")
def _():
    api_cfg = {"api_base": "http://127.0.0.1:1/v1"}
    for payload in ({}, {"choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]}):
        with patch.object(ad, "_post_json", return_value=payload):
            try:
                ad.verify_api_driver(api_cfg)
                raise AssertionError("无效 API 探针结果被接受")
            except ValueError:
                pass
    with patch.object(ad, "_post_json", return_value={"choices": [{
        "message": {"content": "完成", "tool_calls": [{"id": "a", "function": {"name": "read_save_file", "arguments": "{}"}}]},
        "finish_reason": "tool_calls",
    }]}):
        try:
            ad.run_openai_compat_loop(api_cfg, "", [], None, tools=[], max_steps=1)
            raise AssertionError("工具循环耗尽不能算成功")
        except RuntimeError as exc:
            assert "步数上限" in str(exc)
    for rc, reply, expected in ((1, "RPG_VERIFY_OK", False), (0, "login required", False), (0, "RPG_VERIFY_OK", True)):
        version = subprocess.CompletedProcess([], rc, "mock 1.0", "")
        with patch.object(cd, "check_cli_available", return_value=(True, "mock")), \
             patch.object(cd.subprocess, "run", return_value=version), \
             patch.object(cd, "run_cli_turn", return_value={"text": reply}):
            try:
                actual = cd.verify_cli_driver({"cli_tool": "mock"})["ok"]
            except ValueError:
                actual = False
            assert actual == expected


@case("OpenAI 通用协议与 Anthropic 原生缓存识别：端点规范化、流式中途报错捕获、reasoning_content 隔离与工具白名单/坏 JSON 防护")
def _():
    sd = fresh()
    # 1. 显式 openai/anthropic 优先；auto 或旧配置下仅 api.anthropic.com 与 /messages 走原生协议保留 Prompt Cache，其余一律走 OpenAI /chat/completions
    assert ad._detect_api_format({"api_format": "openai", "api_base": "https://api.anthropic.com/v1"}) == "openai"
    assert ad._detect_api_format({"api_format": "auto", "api_base": "https://api.anthropic.com/v1"}) == "anthropic"
    assert ad._detect_api_format({"api_base": "https://api.anthropic.com/v1"}) == "anthropic"
    assert ad._detect_api_format({"api_format": "auto", "api_base": "https://api.deepseek.com/v1"}) == "openai"
    assert ad._detect_api_format({"api_format": "anthropic", "api_base": "https://my-proxy.example/v1"}) == "anthropic"
    assert ad._detect_api_format({"api_format": "auto", "api_base": "https://my-proxy.example/v1/messages"}) == "anthropic"

    # 2. URL 规范化（根域名补 /v1/chat/completions，带子路径补 /chat/completions，完整路径不重复拼接）
    assert ad._openai_chat_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions"
    assert ad._openai_chat_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert ad._openai_chat_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/chat/completions"
    assert ad._openai_chat_url("https://generativelanguage.googleapis.com/v1beta/openai") == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    # 3. SSE 流式中途返回 {"error": ...} 时必须抛出真实错误，不能静默吞成空正文
    orig_sse = ad._iter_sse_json_packets
    try:
        ad._iter_sse_json_packets = lambda *_a, **_kw: iter([{"error": {"message": "Insufficient Balance"}}])
        try:
            ad._execute_step_streaming("http://127.0.0.1:9/v1/chat/completions", {}, {"model": "m"}, "front", lambda *_: None, None, None)
            raise AssertionError("流式中途报错应当抛出异常")
        except RuntimeError as exc:
            assert "Insufficient Balance" in str(exc), exc
    finally:
        ad._iter_sse_json_packets = orig_sse

    # 4. reasoning_content 仅在接口返回 reasoning_content 或 reasoner 模型时回传；同时校验阶段工具白名单与坏 JSON 拦截
    sent_payloads = []
    exec_calls = []
    orig_post, orig_exec = ad._post_json, ad.execute_tool

    replies = [
        # 第 1 步：正文里含 <think>（非 API reasoning_content），且幻觉调用未授权的 apply_changes + 坏 JSON 的 read_save_file
        {
            "choices": [{
                "message": {
                    "content": "<think>内部盘算</think>",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "apply_changes", "arguments": '{"ops":[]}'}},
                        {"id": "c2", "type": "function", "function": {"name": "read_save_file", "arguments": '{"rel_path": '}},
                    ],
                },
                "finish_reason": "tool_calls",
            }]
        },
        # 第 2 步：带 Markdown 代码围栏的合法工具调用 + 真正的 reasoning_content
        {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "API原生思考链",
                    "tool_calls": [
                        {"id": "c3", "type": "function", "function": {"name": "read_save_file", "arguments": '```json\n{"rel_path": "world/now.md"}\n```'}},
                    ],
                },
                "finish_reason": "tool_calls",
            }]
        },
        # 第 3 步：返回最终正文
        {"choices": [{"message": {"content": "雨停了。"}, "finish_reason": "stop"}]},
    ]

    def fake_post(url, headers, payload, timeout=150.0):
        import copy
        sent_payloads.append(copy.deepcopy(payload))
        return replies.pop(0)

    def spy_exec(save_sd, fn, args, **kw):
        exec_calls.append((fn, args))
        return orig_exec(save_sd, fn, args, **kw)

    ad._post_json = fake_post
    ad.execute_tool = spy_exec
    try:
        out = ad.run_openai_compat_loop(
            {"type": "api", "api_format": "openai", "api_base": "https://api.anthropic.com/v1", "api_key": "sk-ant", "model": "claude-sonnet-4-6"},
            "系统提示",
            [{"role": "user", "content": "看看四周"}],
            sd,
            role="front",
            tools=ad.FRONT_INLINE_TOOLS_OPENAI,
        )
    finally:
        ad._post_json, ad.execute_tool = orig_post, orig_exec

    assert out["text"] == "雨停了。"
    assert exec_calls == [("read_save_file", {"rel_path": "world/now.md"})], f"未授权工具与坏 JSON 绝不能进入 execute_tool: {exec_calls}"
    step2_msgs = sent_payloads[1]["messages"]
    first_asst = next(m for m in step2_msgs if m["role"] == "assistant")
    assert "reasoning_content" not in first_asst, "<think> 标签不应伪装成 reasoning_content 发给严格 OpenAI 兼容网关"
    tool_errs = [m["content"] for m in step2_msgs if m["role"] == "tool"]
    assert any("未开放工具" in t for t in tool_errs) and any("JSON 格式不合法" in t for t in tool_errs), tool_errs
    step3_asst = [m for m in sent_payloads[2]["messages"] if m["role"] == "assistant"][-1]
    assert step3_asst.get("reasoning_content") == "API原生思考链"


@case("SDK、依赖与权限沙箱加固：子进程 venv 注入、事件循环兼容、幕后文件 ACL 与静态目录防穿越")
def _():
    import asyncio
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    import ai_log
    from engine.runtime import harness_driver as hd
    import web.server as ws

    sd = fresh("xianxia")
    # 1. 子进程环境自动去重前置当前 Python 解释器目录（即使原本排在 PATH 后方也会被移到首位）
    py_dir = str(Path(sys.executable).resolve().parent)
    cenv = ai_log.child_env_with_trace({"PATH": os.pathsep.join(["C:\\fake_first_bin", py_dir, "C:\\other_bin"])})
    parts = cenv["PATH"].split(os.pathsep)
    assert parts[0] == py_dir and parts.count(py_dir) == 1 and cenv["PYTHONUTF8"] == "1", parts

    # 2. Harness 异步协程在已有运行中 event loop 的线程内调用不崩溃，且注册了 openai_agents_sdk
    har_ids = {h["id"] for h in hd.HARNESS_REGISTRY}
    assert "openai_agents_sdk" in har_ids

    async def _inside_running_loop():
        return hd._run_coro_sync(asyncio.sleep(0, result="loop_ok"))

    assert asyncio.run(_inside_running_loop()) == "loop_ok"

    # 3. 权限隔离：台前与调教助手禁止直读世界包 backstage.md，调教助手禁止展开 save:world/secrets/
    (sd / "world/secrets").mkdir(parents=True, exist_ok=True)
    (sd / "world/secrets/01-暗线.md").write_text("TOP_SECRET_PLOT", encoding="utf-8")
    deny_front_pack = ad.execute_tool(sd, "read_save_file", {"rel_path": "packs/xianxia/backstage.md"}, role="front")
    allow_back_pack = ad.execute_tool(sd, "read_save_file", {"rel_path": "packs/xianxia/backstage.md"}, role="back")
    deny_tuner_pack = ad.execute_tool(sd, "read_tuner_target", {"target": "pack:backstage.md"}, role="tuner", tuner_context={"pack_id": "xianxia"})
    deny_tuner_sec = ad.execute_tool(sd, "read_tuner_target", {"target": "save:world/secrets/01-暗线.md"}, role="tuner", tuner_context={"pack_id": "xianxia"})
    assert "权限隔离" in deny_front_pack and len(allow_back_pack) > 20
    assert "剧透" in deny_tuner_pack and "TOP_SECRET_PLOT" not in deny_tuner_sec

    # 4. Claude CLI 权限模式：只读回合不加 --permission-mode，带写权限回合自动加 acceptEdits 防后台挂起
    cmd_ro, _, _ = cd._build_cli_invocation("claude.exe", "claude", "hi", [], tools="Read,Grep,Glob")
    cmd_rw, _, _ = cd._build_cli_invocation("claude.exe", "claude", "hi", [], tools="Read,Write,Edit,Bash")
    assert "--permission-mode" not in cmd_ro
    assert cmd_rw[cmd_rw.index("--permission-mode") + 1] == "acceptEdits"

    # 5. Stdlib HTTP 静态文件路由拦截 `..` 与 URL 编码目录穿越
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ws.StdlibRPGHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for bad_path in ("/static/../../config.yaml", "/static/%2e%2e/%2e%2e/config.yaml"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{bad_path}", timeout=5)
                raise AssertionError(f"路径穿越未被拦截: {bad_path}")
            except urllib.error.HTTPError as e:
                assert e.code in (403, 404), (bad_path, e.code)
    finally:
        srv.shutdown()


@case("全审查项回归：OpenAI Agents SDK 真工具/禁遥测/零全局污染、全 CLI 只读防双写、轻量标记与 world_sim.py")
def _():
    import tempfile
    import types
    import start

    sd = fresh()
    # 1. 同名假 `agents` 包绝不能被误判为已安装 `openai-agents`
    orig_agents = sys.modules.get("agents")
    orig_openai = sys.modules.get("openai")
    try:
        sys.modules["agents"] = types.ModuleType("agents")
        assert ad._has_openai_agents_sdk() is False

        # 2. 模拟真实 `openai-agents` SDK：校验 /chat/completions 模型封装、禁遥测、不改 os.environ、挂载沙箱工具并写回状态
        sdk_calls = {}

        class FakeAsyncOpenAI:
            def __init__(self, api_key=None, base_url=None, http_client=None):
                sdk_calls["api_key"] = api_key
                sdk_calls["base_url"] = base_url
                sdk_calls["closed"] = False
                self.http_client = http_client
                assert not http_client.follow_redirects

            async def close(self):
                await self.http_client.aclose()
                sdk_calls["closed"] = True

        class FakeChatCompletionsModel:
            def __init__(self, model, openai_client):
                self.model = model
                self.openai_client = openai_client
                sdk_calls["chat_model"] = model

        class FakeFunctionTool:
            def __init__(self, name, description, params_json_schema, on_invoke_tool, strict_json_schema=False):
                self.name = name
                self.description = description
                self.params_json_schema = params_json_schema
                self.on_invoke_tool = on_invoke_tool
                self.strict_json_schema = strict_json_schema

        class FakeRunConfig:
            def __init__(self, tracing_disabled=False):
                self.tracing_disabled = tracing_disabled

        class FakeAgent:
            def __init__(self, name, instructions, model, tools=None):
                self.name = name
                self.instructions = instructions
                self.model = model
                self.tools = tools or []

        class FakeRunner:
            @staticmethod
            async def run(agent, input="", max_turns=12, run_config=None):
                sdk_calls["tracing_disabled"] = getattr(run_config, "tracing_disabled", None)
                sdk_calls["tool_names"] = [t.name for t in agent.tools]
                sdk_calls["uses_chat_completions_model"] = isinstance(agent.model, FakeChatCompletionsModel)
                read_tool = next(t for t in agent.tools if t.name == "read_save_file")
                sdk_calls["read_result"] = await read_tool.on_invoke_tool(None, '{"rel_path": "world/now.md"}')
                out = (
                    "霓虹灯在雨雾中闪烁。\n\n"
                    '<state_changes>\n[{"op":"set","file":"world/now.md","key":"ui_mood","value":"mystery"}]\n</state_changes>'
                )
                return types.SimpleNamespace(final_output=out)

        fake_agents_mod = types.ModuleType("agents")
        fake_agents_mod.Agent = FakeAgent
        fake_agents_mod.Runner = FakeRunner
        fake_agents_mod.FunctionTool = FakeFunctionTool
        fake_agents_mod.RunConfig = FakeRunConfig
        fake_agents_mod.OpenAIChatCompletionsModel = FakeChatCompletionsModel
        fake_openai_mod = types.ModuleType("openai")
        fake_openai_mod.AsyncOpenAI = FakeAsyncOpenAI

        sys.modules["agents"] = fake_agents_mod
        sys.modules["openai"] = fake_openai_mod
        assert ad._has_openai_agents_sdk() is True

        old_env_key = os.environ.get("OPENAI_API_KEY")
        old_env_url = os.environ.get("OPENAI_BASE_URL")
        os.environ["OPENAI_API_KEY"] = "ambient-test-api-key"
        os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
        orig_load = om.load_config
        base_cfg = orig_load()
        om.load_config = lambda: {
            **base_cfg,
            "front_driver": {
                "type": "harness",
                "harness_mode": "openai_agents_sdk",
                "api_base": "https://api.deepseek.com/v1",
                "api_key": "sk-deepseek-harness",
                "model": "deepseek-chat",
            },
            "back_driver": {"inherit_front": True},
        }
        try:
            streamed = []
            turn_res = Orch()._run_turn_core("观察街角", save_name=SAVE, stream=True, on_delta=streamed.append)
        finally:
            om.load_config = orig_load
            if old_env_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_env_key
            if old_env_url is None:
                os.environ.pop("OPENAI_BASE_URL", None)
            else:
                os.environ["OPENAI_BASE_URL"] = old_env_url

        assert sdk_calls["api_key"] == "sk-deepseek-harness"
        assert sdk_calls["base_url"] == "https://api.deepseek.com/v1"
        assert sdk_calls["uses_chat_completions_model"] is True
        assert sdk_calls["tracing_disabled"] is True
        assert sdk_calls["closed"] is True
        assert "location:" in sdk_calls["read_result"]
        assert turn_res["story"] == "霓虹灯在雨雾中闪烁。" and "".join(streamed) == "霓虹灯在雨雾中闪烁。"
        assert common.read_frontmatter(sd / "world/now.md")[0]["ui_mood"] == "mystery"
    finally:
        if orig_agents is None:
            sys.modules.pop("agents", None)
        else:
            sys.modules["agents"] = orig_agents
        if orig_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = orig_openai

    # 3. Gemini / OpenCode / Antigravity 只读限制与防越权双写回退，非 Claude CLI 剔除 Agent 工具名
    gem_ro, gem_stdin_ro, _ = cd._build_cli_invocation("gemini", "gemini", "测试", [], tools="Read,Grep,Glob")
    gem_rw, _, _ = cd._build_cli_invocation("gemini", "gemini", "测试", [], tools="Read,Write,Edit,Bash")
    assert "-y" not in gem_ro and "只读回合沙箱" in gem_stdin_ro
    assert "-y" in gem_rw

    cli_seen = []
    orig_cli, orig_load2 = om.run_cli_turn, om.load_config
    cfg2 = orig_load2()
    om.load_config = lambda: {**cfg2, "front_driver": {"type": "cli", "cli_tool": "gemini", "cli_args": []}}

    def rogue_cli(driver_cfg, prompt_text, **kw):
        cli_seen.append(kw)
        if kw.get("replace_system"):
            # 模拟第三方 CLI 越权直接篡改存档文件，同时又返回 <state_changes>
            (sd / "world/lore.md").write_text("ROGUE_DIRECT_WRITE", encoding="utf-8")
            return {
                "text": '街面平静。\n<state_changes>\n[{"op":"set","file":"world/now.md","key":"ui_mood","value":"combat"}]\n</state_changes>',
                "model": "gemini",
            }
        return {"text": "GM 已执行。", "model": "gemini"}

    om.run_cli_turn = rogue_cli
    try:
        Orch()._run_turn_core("巡视街面", save_name=SAVE)
        Orch()._run_turn_core("/gm 检查时间线", save_name=SAVE)
    finally:
        om.run_cli_turn, om.load_config = orig_cli, orig_load2

    assert "ROGUE_DIRECT_WRITE" not in (sd / "world/lore.md").read_text(encoding="utf-8"), "只读回合越权直改应被引擎自动还原"
    assert common.read_frontmatter(sd / "world/now.md")[0]["ui_mood"] == "combat", "合法变更块仍正常生效"
    assert "Agent" not in cli_seen[1]["tools"], f"非 Claude CLI 不应传入 Agent 工具名: {cli_seen[1]['tools']}"

    # 4. start.py 轻量模式标记（.minimal_ok）就绪后不再每次重跑 pip 安装
    with tempfile.TemporaryDirectory() as tmp_venv_parent:
        fake_venv = Path(tmp_venv_parent) / ".venv"
        fake_py = fake_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_bytes(b"")
        marker = fake_venv / ".minimal_ok"
        marker.write_text("pyyaml\n", encoding="utf-8")
        orig_v, orig_m, orig_hm, orig_inst = start.VENV, start.MINIMAL_MARKER, start.has_modules, start.install_requirements
        start.VENV = fake_venv
        start.MINIMAL_MARKER = marker
        start.has_modules = lambda py, mods=start.REQUIRED_MODULES: tuple(mods) == tuple(start.MINIMAL_MODULES)
        start.install_requirements = lambda _py: (_ for _ in ()).throw(AssertionError("轻量模式已就绪时不应再次触发安装"))
        try:
            assert start.prepare_python(force_venv=True) == fake_py
        finally:
            start.VENV, start.MINIMAL_MARKER, start.has_modules, start.install_requirements = orig_v, orig_m, orig_hm, orig_inst

    # 5. AGENTS.md 入口完整性与 world_sim.py CLI 工具可用性
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for required_ref in ("engine/agents/world-sim.md", "engine/agents/tuner-agent.md", "engine/commands/", "engine/tools/world_sim.py"):
        assert required_ref in agents_md, f"AGENTS.md 缺少引用: {required_ref}"
    ws_help = subprocess.run([sys.executable, "engine/tools/world_sim.py", "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert ws_help.returncode == 0 and "--task" in ws_help.stdout, ws_help.stderr


for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
