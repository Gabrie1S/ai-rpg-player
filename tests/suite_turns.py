"""一轮投喂和交付方式的回归测试：提示词装什么、一次写完一轮、变更和幕后推演的时机。只应由 tests/run_all.py 在临时副本中运行。"""
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import apply as apply_tool  # noqa: E402
import common  # noqa: E402
import context as ctxm  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
import turn_protocol as tp  # noqa: E402
from engine.runtime import cli_driver as cd  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
SAVE, PACK = "_suite_turns", "xianxia"
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


class ScriptedCli:
    """替换 run_cli_turn：记下提示词，按顺序返回写好的回复。"""

    def __init__(self, *replies, plan_ready=True):
        self.replies = list(replies)
        self.calls = []
        self.plan_ready = plan_ready

    def __enter__(self):
        self.saved = (om.run_cli_turn, om.load_config, om.plot.needs_planning)
        if self.plan_ready:
            om.plot.needs_planning = lambda _sd: False
        cfg = dict(om.load_config())
        cfg["front_driver"] = {"type": "cli", "cli_tool": "claude", "cli_args": []}
        cfg["back_driver"] = {"inherit_front": True}
        om.load_config = lambda: cfg

        def fake(driver, prompt, **kw):
            self.calls.append({"prompt": prompt, "role": kw.get("role"), **kw})
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            if kw.get("stream") and kw.get("on_delta"):
                for i in range(0, len(reply), 7):
                    kw["on_delta"](reply[i:i + 7])
            return {"text": reply, "tool_logs": [], "model": "fake"}

        om.run_cli_turn = fake
        return self

    def __exit__(self, *_):
        om.run_cli_turn, om.load_config, om.plot.needs_planning = self.saved


def story(text, ops="[]"):
    return f"{text}\n\n<state_changes>\n{ops}\n</state_changes>"


# ---------------- 变更块 ----------------
@case("变更块：正常、缺失、带代码围栏、写坏时都不丢正文")
def _():
    text, ops, err = tp.split_changes(story("正文", '[{"op":"set"}]'))
    assert (text, ops, err) == ("正文", [{"op": "set"}], "")
    assert tp.split_changes("只有正文") == ("只有正文", [], "")
    text, ops, err = tp.split_changes("正文\n<state_changes>\n```json\n[]\n```\n</state_changes>")
    assert (text, ops, err) == ("正文", [], "")
    text, ops, err = tp.split_changes("正文\n<state_changes>\n[{坏的\n</state_changes>")
    assert text == "正文" and not ops and "JSON" in err
    text, ops, err = tp.split_changes('正文\n<state_changes>\n[{"op":"set"}]')  # 输出被截断，没有结束标签
    assert text == "正文" and ops == [{"op": "set"}]


@case("流式过滤：标签被拆开时玩家也看不到变更块，半截像标签的正文不丢")
def _():
    full = story("你走进门。a<b 也算正文。", '[{"op":"set","file":"x"}]')
    for size in (1, 2, 5, 13, 500):
        seen = []
        f = tp.ChangesStreamFilter(seen.append)
        for i in range(0, len(full), size):
            f.feed(full[i:i + size])
        f.finish()
        out = "".join(seen)
        assert out == "你走进门。a<b 也算正文。", (size, out)
    seen = []
    f = tp.ChangesStreamFilter(seen.append)
    f.feed("没有变更块，结尾是 <sta")
    f.finish()
    assert "".join(seen) == "没有变更块，结尾是 <sta"


# ---------------- 一次写完一轮 ----------------
@case("普通回合：一次调用完成，只读工具，静态内容走系统提示，变更写入存档，正文干净")
def _():
    sd = fresh()
    ops = json.dumps([
        {"op": "set", "file": "world/now.md", "key": "ui_mood", "value": "rest"},
        {"op": "advance_days", "days": 3, "reason": "赶路"},
    ], ensure_ascii=False)
    with ScriptedCli(story("你走了三天。", ops)) as cli:
        streamed = []
        res = Orch()._run_turn_core("赶路去七里集", save_name=SAVE, stream=True, on_delta=streamed.append)
    assert len(cli.calls) == 1
    call = cli.calls[0]
    assert call["tools"] == "Read,Grep,Glob" and call["replace_system"] and not call["needs_project"], call["tools"]
    assert "state_changes" in call["system_prompt"] and "这一轮怎么交付" in call["system_prompt"]
    assert "写作标准" in call["system_prompt"] and "写作标准" not in call["prompt"], "静态内容不该放在每轮消息里"
    assert "赶路去七里集" in call["prompt"]
    assert res["story"] == "你走了三天。" and "".join(streamed) == "你走了三天。", (res["story"], streamed)
    meta, _ = common.read_frontmatter(sd / "world/now.md")
    assert meta["ui_mood"] == "rest" and meta["day"] == 3, meta
    turns = om.parse_story_History(sd)
    assert turns[-1]["story"] == "你走了三天。" and "state_changes" not in turns[-1]["story"], turns[-1]
    log = subprocess.run(["git", "-C", str(sd), "log", "--format=%s"], capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    assert len([l for l in log if l.startswith("[turn]")]) == 1, log


@case("世界包命令也一次写完；/gm 这类要改文件的命令才给完整工具")
def _():
    fresh()
    with ScriptedCli(story("你看见了三种结局。"), "已修改。") as cli:
        orch = Orch()
        orch._run_turn_core("/yixiantian 要不要跟他走", save_name=SAVE)
        orch._run_turn_core("/gm 把今天改成晴天", save_name=SAVE)
    assert cli.calls[0]["tools"] == "Read,Grep,Glob" and "因果链" in cli.calls[0]["prompt"], "发动能力时才带上规则"
    assert "Agent" in cli.calls[1]["tools"] and cli.calls[1]["needs_project"] and not cli.calls[1]["replace_system"]
    assert "apply.py" in cli.calls[1]["system_prompt"]


@case("变更写失败：正文保住，下一轮告诉模型原因，改好后不再提")
def _():
    sd = fresh()
    bad = json.dumps([{"op": "replace", "file": "world/now.md", "old": "不存在的文字", "new": "x"}], ensure_ascii=False)
    good = json.dumps([{"op": "set", "file": "world/now.md", "key": "ui_mood", "value": "rest"}])
    with ScriptedCli(story("第一轮。", bad), story("第二轮。", good), story("第三轮。")) as cli:
        orch = Orch()
        assert orch._run_turn_core("行动一", save_name=SAVE)["story"] == "第一轮。"
        assert (sd / "log/pending_turn.json").exists()
        assert om.parse_story_History(sd)[-1]["story"] == "第一轮。"
        orch._run_turn_core("行动二", save_name=SAVE)
        assert "没写进存档" in cli.calls[1]["prompt"] and "不存在的文字" in cli.calls[1]["prompt"]
        assert not (sd / "log/pending_turn.json").exists()
        orch._run_turn_core("行动三", save_name=SAVE)
        assert "没写进存档" not in cli.calls[2]["prompt"]
    status = subprocess.run(["git", "-C", str(sd), "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8").stdout
    assert "pending_turn" not in status


@case("变更块写坏：正文保住，错误留到下一轮")
def _():
    sd = fresh()
    with ScriptedCli("正文。\n<state_changes>\n[{坏的\n</state_changes>", story("下一轮。")) as cli:
        orch = Orch()
        assert orch._run_turn_core("行动", save_name=SAVE)["story"] == "正文。"
        assert json.loads((sd / "log/pending_turn.json").read_text(encoding="utf-8"))["errors"]
        orch._run_turn_core("行动", save_name=SAVE)
        assert "不是合法的 JSON" in cli.calls[1]["prompt"]


@case("推进时间：冻结的世界拒绝，事务回滚")
def _():
    sd = fresh()
    path = sd / "world/now.md"
    meta, body = common.read_frontmatter(path)
    meta["frozen"] = True
    common.write_frontmatter(path, meta, body)
    res = apply_tool.apply_operations(sd, [{"op": "advance_days", "days": 2}], commit_checkpoint=False)
    assert not res["ok"] and common.read_frontmatter(path)[0]["day"] == 0


# ---------------- 幕后推演的时机 ----------------
@case("幕后：到了间隔才跑，在这一轮开始前跑完，简报交给台前，记下推演到哪天")
def _():
    sd = fresh()
    with ScriptedCli(story("平常的一轮。", '[{"op":"advance_days","days":7}]'), "简报：势力有动静", story("下一轮。")) as cli:
        orch = Orch()
        orch._run_turn_core("闭关七天", save_name=SAVE)
        assert len(cli.calls) == 1, "这一轮写完不等幕后"
        orch._run_turn_core("出关", save_name=SAVE)
    assert [c["role"] for c in cli.calls] == ["front", "back", "front"]
    assert "第1天 ~ 第7天" in cli.calls[1]["prompt"]
    assert "简报：势力有动静" in cli.calls[2]["prompt"]
    meta, _ = common.read_frontmatter(sd / "world/now.md")
    assert meta["last_tick_day"] == 7


@case("幕后：造物请求留到下一轮前做；幕后失败不挡住回合，请求保留")
def _():
    sd = fresh()
    ops = json.dumps([{"op": "request_sim", "task": "造物", "detail": "黑水渡的详情"}], ensure_ascii=False)
    with ScriptedCli(story("你动身去黑水渡。", ops), RuntimeError("幕后挂了"), story("继续。"), "简报", story("到了。")) as cli:
        orch = Orch()
        orch._run_turn_core("去黑水渡", save_name=SAVE)
        assert json.loads((sd / "log/pending_turn.json").read_text(encoding="utf-8"))["sims"]
        assert orch._run_turn_core("继续走", save_name=SAVE)["story"] == "继续。"
        assert json.loads((sd / "log/pending_turn.json").read_text(encoding="utf-8"))["sims"], "失败的请求要保留"
        orch._run_turn_core("到了吗", save_name=SAVE)
    assert "黑水渡的详情" in cli.calls[3]["prompt"] and cli.calls[3]["role"] == "back"
    assert not (sd / "log/pending_turn.json").exists()


@case("近期固定事件提示给台前；已过的和太远的不提")
def _():
    sd = fresh()
    timeline = sd / "world/events/timeline.yaml"
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_text(yaml.safe_dump({"events": [
        {"id": "e1", "title": "近的大事", "day": 5, "status": "planned"},
        {"id": "e2", "title": "太远的事", "day": 400, "status": "planned"},
        {"id": "e3", "title": "结算过的事", "day": 6, "status": "done"},
    ]}, allow_unicode=True), encoding="utf-8")
    note = ctxm.build_authors_note(sd)
    assert "近的大事" in note and "太远的事" not in note and "结算过的事" not in note


# ---------------- 提示词里装什么 ----------------
@case("台前提示词：不带调教助手和可选能力规则，规则书只带常驻部分；幕后拿完整规则书")
def _():
    sd = fresh()
    front = ctxm.build_system_prompt(sd, role="front", protocol="inline")
    assert "场外调教技能" not in front and "蒙太奇" not in front
    assert "判定依据" in front and "灵根多为" not in front
    assert "packs/xianxia/rules.md" in front and "灵根" in front.split("其余章节用到时再读")[1][:200]
    assert "skills/causal-foresight/SKILL.md" in front
    back = ctxm.build_system_prompt(sd, role="back")
    assert "## 4. 灵根" in back
    ui = ctxm.build_system_prompt(sd, role="ui")
    assert "写作标准" not in ui and "pixel_frame" in ui
    genesis = ctxm.build_system_prompt(None, role="genesis")
    assert "Pack Porter" not in genesis and "世界包设计规范" in genesis


@case("可选能力规则：说到或用命令才放进这一轮")
def _():
    sd = fresh()
    assert "skills/causal-foresight/SKILL.md" not in ctxm.assemble_turn_context(sd, "四处看看")["files"]
    assert "skills/causal-foresight/SKILL.md" in ctxm.assemble_turn_context(sd, "我想用一线天看看")["files"]
    assert "skills/causal-foresight/SKILL.md" in ctxm.assemble_turn_context(sd, "/yixiantian 跟不跟他走")["files"]


@case("上下文体积：索引、日志、人物记忆会变长，放进每轮的部分有上限，文件本身不动")
def _():
    sd = fresh()
    many = "\n".join(f"- 第{i}条" for i in range(500))
    (sd / "player/journal.md").write_text(f"# 日志\n\n## 听到的传闻\n{many}\n\n## 当前目标\n- 活下去\n", encoding="utf-8")
    (sd / "world/index.md").write_text(f"# 索引\n\n## 主角见过的人\n{many}\n", encoding="utf-8")
    npc = sd / "world/npcs/npc_tmp.md"
    npc.write_text(f"---\nname: 临时\n---\n\n## 性格\n- 稳重\n\n## 认知\n{many}\n\n## 重要时刻（永不压缩）\n{many}\n", encoding="utf-8")
    now = sd / "world/now.md"
    meta, body = common.read_frontmatter(now)
    meta["present"] = ["npc_tmp"]
    common.write_frontmatter(now, meta, body)
    files = ctxm.assemble_turn_context(sd, "看看")["files"]
    journal, index, card = files["player/journal.md"], files["world/index.md"], files["world/npcs/npc_tmp.md"]
    assert "第499条" in journal and "第0条\n" not in journal and "活下去" in journal and "用到时再读" in journal
    assert "第0条" in index and "第499条" not in index, "索引保留最前面的（最近见过的）"
    assert "稳重" in card and "第0条" in card.split("## 重要时刻")[1], "重要时刻永不压缩"
    assert len(journal) < 500 and "第499条" in (sd / "player/journal.md").read_text(encoding="utf-8")
    big = sd / "world/design/大文件.md"
    big.write_text("字" * 30000, encoding="utf-8")
    assert len(ctxm._fit_for_context("world/design/大文件.md", big.read_text(encoding="utf-8"))) < 8200


@case("最近回合：最新几轮放原文，更早的只留一句")
def _():
    sd = fresh()
    for i in range(6):
        apply_tool.archive_story_turn(sd, f"行动{i}", f"第{i}轮开头。\n\n这是第{i}轮的详细经过，很长很长。\n\n第{i}轮结尾。")
    md, messages = Orch._recent_turns(sd, {"context": {"sliding_window_turns": 8, "recent_full_turns": 3}})
    assert "这是第5轮的详细经过" in md and "这是第3轮的详细经过" in md
    assert "这是第2轮的详细经过" not in md and "行动2" in md and "第2轮结尾" in md
    assert len(messages) == 6 and "行动0" in messages[0]["content"], "接口模式也要带上更早的摘要"


# ---------------- 故事原文、翻页和章节摘要 ----------------
import chapters as chapter_log  # noqa: E402
import story_log  # noqa: E402


def archive_many(sd, n, start=0):
    for i in range(start, start + n):
        apply_tool.archive_story_turn(sd, f"行动{i + 1}", f"第{i + 1}轮的经过。\n\n第{i + 1}轮结尾。")


@case("故事原文：编号连续，往前翻页，新写入后缓存跟着更新")
def _():
    sd = fresh()
    archive_many(sd, 25)
    turns = story_log.all_turns(sd)
    assert [t["index"] for t in turns] == list(range(1, 26))
    page = om.parse_story_History(sd, limit=20)
    assert page[0]["index"] == 6 and page[-1]["index"] == 25
    older = Orch().get_story_page(SAVE, before=6, limit=20)
    assert [t["index"] for t in older["turns"]] == [1, 2, 3, 4, 5] and older["total"] == 25
    archive_many(sd, 1, start=25)
    assert story_log.count_turns(sd) == 26 and story_log.all_turns(sd)[-1]["input"] == "行动26"
    hud = Orch().get_hud_state(SAVE)
    assert hud["story_total"] == 26 and len(hud["story_turns"]) == 20 and hud["story_turns"][-1]["index"] == 26


@case("章节摘要：攒够一章由模型在后台写，带覆盖标记，进下一轮的前情；最近回合和摘要之间不断档")
def _():
    sd = fresh()
    archive_many(sd, 14)
    md, _ = Orch._recent_turns(sd, {"context": {}})
    assert "行动1 →" in md and "第14轮的经过" in md, "还没有摘要时，最早的回合也要有一句话"
    with ScriptedCli(story("第15轮正文。"), "柴刀与野猪\n\n你在竹山上遇到了野猪，爹赶来解围。") as cli:
        orch = Orch()
        orch._run_turn_core("行动15", save_name=SAVE)
        orch._chapter_thread.join(30)
    summary_call = cli.calls[1]
    assert summary_call["role"] == "summary" and summary_call["tools"] == "" and "第 1~12 轮" in summary_call["prompt"]
    text = (sd / "log/summaries.md").read_text(encoding="utf-8")
    assert "<!-- covers:1-12 -->" in text and "柴刀与野猪" in text
    assert chapter_log.covered_until(sd, 15) == 12
    files = ctxm.assemble_turn_context(sd, "看看")["files"]
    assert "爹赶来解围" in files["log/summaries.md（前情）"] and "covers" not in files["log/summaries.md（前情）"]
    md, _ = Orch._recent_turns(sd, {"context": {}})
    assert "第15轮正文" in md and "行动9 →" in md


@case("章节摘要：模型写不了时退回自动摘录，不断档；撤回到摘要之前时不再用那一章")
def _():
    sd = fresh()
    archive_many(sd, 14)
    with ScriptedCli(story("第15轮正文。"), RuntimeError("额度用完了")):
        orch = Orch()
        orch._run_turn_core("行动15", save_name=SAVE)
        orch._chapter_thread.join(30)
    assert "自动摘录" in (sd / "log/summaries.md").read_text(encoding="utf-8")
    assert chapter_log.covered_until(sd, 15) == 12
    assert chapter_log.covered_until(sd, 10) == 0 and not chapter_log.read_chapters(sd, 10)


@case("前情的体积有上限：玩得再久，每轮带的摘要也不会一直变大")
def _():
    sd = fresh()
    turns = [{"index": i, "date_header": f"第{i}天", "input": "x", "story": "y"} for i in range(1, 1201)]
    for n in range(100):
        chunk = turns[n * 12:(n + 1) * 12]
        chapter_log.append_chapter(sd, chunk, f"第{n}章标题", "正文" * 200)
    block = chapter_log.context_block(sd, 1200)
    assert len(block) <= chapter_log.CONTEXT_MAX_CHARS + 20 and "第99章标题" in block


# ---------------- 命令行参数和输出解析 ----------------
class FakeProcess:
    def __init__(self, code, flags=("--output-format", "stream-json")):
        self.code, self.flags = code, flags

    def __enter__(self):
        self.saved = (cd._build_cli_invocation, cd.check_cli_available)
        cd._build_cli_invocation = lambda *_a: ([sys.executable, "-c", self.code, *self.flags], None, None)
        cd.check_cli_available = lambda *_a: (True, "fake")

    def __exit__(self, *_):
        cd._build_cli_invocation, cd.check_cli_available = self.saved


def stream_script(events):
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    return "import sys\nsys.stdout.reconfigure(encoding='utf-8')\nfor line in %r:\n    print(line, flush=True)\n" % (lines,)


def delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}


START = {"type": "stream_event", "event": {"type": "message_start"}}


@case("流式解析：真正逐字显示，读文件前的文字被撤掉，最终正文和用量都拿到")
def _():
    events = [
        {"type": "system", "subtype": "init"}, START, delta("让我查一下"),
        {"type": "assistant"}, START, delta("正文"), delta("第二段"),
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想法不外露"}}},
        {"type": "result", "result": "正文第二段", "usage": {"input_tokens": 3, "output_tokens": 9}, "num_turns": 2},
    ]
    seen, resets = [], []
    logged = []
    saved_log = cd.log_event
    cd.log_event = lambda ev, **kw: logged.append((ev, kw))
    try:
        with FakeProcess(stream_script(events)):
            res = cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="front", stream=True, on_delta=seen.append, on_step_reset=lambda: resets.append(1))
    finally:
        cd.log_event = saved_log
    assert "".join(seen) == "让我查一下正文第二段" and resets == [1]
    assert res["text"] == "正文第二段"
    usage = next(kw for ev, kw in logged if ev == "cli.usage")
    assert usage["input"] == 3 and usage["output"] == 9 and usage["api_calls"] == 2


@case("AI 工具报错（比如额度用完）：抛出原因，不当成正文")
def _():
    events = [{"type": "result", "is_error": True, "result": "额度用完了"}]
    with FakeProcess(stream_script(events)):
        try:
            cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="front", stream=True, on_delta=lambda _t: None)
            raise AssertionError("应当报错")
        except RuntimeError as exc:
            assert "额度用完了" in str(exc)
    code = "print('{\"result\": \"幕后简报\", \"usage\": {}}')"
    with FakeProcess(code, ("--output-format", "json")):
        assert cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="back")["text"] == "幕后简报"


@case("台前思考强度：默认快速，玩家可以改；幕后另算；其他 AI 工具不加这个参数")
def _():
    from engine.runtime.api_driver import front_driver_for_turn, resolve_back_driver

    claude = {"type": "cli", "cli_tool": "claude", "cli_args": []}
    assert front_driver_for_turn({"front_driver": claude})["cli_args"] == ["--effort", "low"]
    assert front_driver_for_turn({"front_driver": {**claude, "cli_effort": "high"}})["cli_args"] == ["--effort", "high"]
    assert front_driver_for_turn({"front_driver": {**claude, "cli_effort": "auto"}})["cli_args"] == []
    assert front_driver_for_turn({"front_driver": {**claude, "cli_args": ["--effort", "medium"]}})["cli_args"] == ["--effort", "medium"]
    assert "--effort" not in (front_driver_for_turn({"front_driver": {"type": "cli", "cli_tool": "codex"}}).get("cli_args") or [])
    assert resolve_back_driver({"front_driver": claude, "back_driver": {"inherit_front": True}})["cli_args"] == ["--effort", "high"], "幕后默认想得更深"


@case("调教助手：知道主角性格这类当前状态要改存档文件（界面上的心性栏读的就是它），不放 mods")
def _():
    fresh()
    with ScriptedCli("改好了。") as cli:
        Orch()._run_tuner_core("把主角的心性改成更依赖爹", save_name=SAVE)
    prompt = cli.calls[0]["prompt"] + (cli.calls[0].get("system_prompt") or "")
    assert "player/persona.md" in prompt and "apply.py" in prompt and "不放 mods" in prompt, prompt[:600]


@case("日志卡：某栏超过上限或条目太长时，下一轮提醒主持人整理；整齐时不提")
def _():
    sd = fresh()
    journal = sd / "player/journal.md"
    tidy = "# 日志\n\n## 认识的人\n- 爹：箍桶匠，主角的养父\n\n## 知道的秘密\n- 爹会飞\n"
    journal.write_text(tidy, encoding="utf-8")
    assert "日志卡该整理了" not in ctxm.build_authors_note(sd)
    many = "\n".join(f"- 秘密{i}" for i in range(12))
    long_line = "- " + "很长" * 50
    journal.write_text(f"# 日志\n\n## 认识的人\n{long_line}\n\n## 知道的秘密\n{many}\n", encoding="utf-8")
    note = ctxm.build_authors_note(sd)
    assert "日志卡该整理了" in note and "知道的秘密（12 条）" in note and "认识的人（1 条，1 条太长）" in note, note
    ctx_journal = ctxm.assemble_turn_context(sd, "看看")["files"]["player/journal.md"]
    assert all(len(line) <= ctxm.JOURNAL_CONTEXT_LINE_CHARS + 1 for line in ctx_journal.split("\n"))


@case("调教助手：调教当前世界时知道改哪个文件（心性、人物、剧情走向），不放 mods")
def _():
    fresh()
    with ScriptedCli("改好了。") as cli:
        Orch()._run_tuner_core("让爹以后对他更严厉一点", save_name=SAVE)
    prompt = cli.calls[0]["prompt"] + (cli.calls[0].get("system_prompt") or "")
    for need in ("player/persona.md", "world/npcs/<id>.md", "world/director.md", "apply.py", "不放 mods"):
        assert need in prompt, need


@case("调教助手：命令行驱动也拿到完整的系统提示，回复不剧透的规则贴在玩家诉求后面")
def _():
    fresh()
    with ScriptedCli("改好了。") as cli:
        Orch()._run_tuner_core("爹的设定写对了吗", save_name=SAVE)
    call = cli.calls[0]
    system = call.get("system_prompt") or ""
    assert "回复不剧透" in system and "真实修为" in system, "系统提示没传给命令行驱动"
    assert call["prompt"].rstrip().endswith("读到了也不要写进回复。"), call["prompt"][-200:]


# ---------------- 剧情规划 ----------------
import plot  # noqa: E402

PLAN = """# 剧情规划

## 当前卷

第一卷 · 离乡。主冲突：要不要离开村子。节拍：铺垫 → [起] → 升级 → 转折 → 高潮 → 余波

## 接下来几章

### 第2章 · 测灵前夜（状态：进行中；预计第 10~20 天）
- 节拍：起。章内先松后紧
- 要发生的事：玩家去镇上 → 撞见测灵使；不去 → 测灵使上门
- 因果来源：宗门下乡排期到了
- 收在哪里：主角决定测不测

### 第3章 · 测灵（状态：待开始）
- 节拍：升级

### 第4章 · 变故（状态：待开始）
- 节拍：转折

## 已完成的章

- 第1章 · 冬月：主角和爹的日常
"""


@case("剧情规划：台前每轮带上当前卷和当前章，后面的章只给标题；够了就不补")
def _():
    sd = fresh()
    path = sd / "world/design/剧情规划.md"
    assert path.exists() and plot.needs_planning(sd), "新存档带着空的规划骨架，需要幕后补"
    path.write_text(PLAN, encoding="utf-8")
    assert not plot.needs_planning(sd)
    brief = ctxm.assemble_turn_context(sd, "看看")["files"]["world/design/剧情规划（当前章）"]
    assert "第一卷 · 离乡" in brief and "测灵前夜" in brief and "撞见测灵使" in brief
    assert "第3章 · 测灵" in brief and "节拍：升级" not in brief, "后面的章只给标题"
    assert "冬月：主角和爹的日常" not in brief, "已完成的章不用带"


@case("剧情规划：不够时在下一轮开始前让幕后补，几轮内不重复补；补够了就不再触发")
def _():
    sd = fresh()
    path = sd / "world/design/剧情规划.md"

    def planner(reply):
        def write(_driver, prompt, **kw):
            path.write_text(PLAN, encoding="utf-8")
            return reply
        return write

    with ScriptedCli("规划简报", story("第一轮。"), story("第二轮。"), plan_ready=False) as cli:
        orch = Orch()
        orch._run_turn_core("行动一", save_name=SAVE)
        assert [c["role"] for c in cli.calls] == ["back", "front"]
        assert "剧情规划不够了" in cli.calls[0]["prompt"] and "规划剧情" in cli.calls[0]["prompt"]
        orch._run_turn_core("行动二", save_name=SAVE)
        assert [c["role"] for c in cli.calls] == ["back", "front", "front"], "五轮内不重复补"
    path.write_text(PLAN, encoding="utf-8")
    with ScriptedCli(story("第三轮。"), plan_ready=False) as cli:
        Orch()._run_turn_core("行动三", save_name=SAVE)
        assert [c["role"] for c in cli.calls] == ["front"], "规划够了就不触发"


@case("剧情规划：幕后、生成世界、台前的提示词都知道怎么做")
def _():
    sd = fresh()
    back = ctxm.build_system_prompt(sd, role="back")
    for need in ("剧情规划（生成世界和每次推演都要做）", "铺垫 → 起 → 升级 → 转折 → 高潮 → 余波", "排期总表", "节奏备忘", "本卷终结"):
        assert need in back, need
    front = ctxm.build_system_prompt(sd, role="front", protocol="inline")
    assert "跟着剧情规划走" in front and '"task":"剧情推进"' in front
    seen = {}

    def sim(_sd, args, _cfg, on_status=None):
        seen["req"] = args["request"]
        return "简报"

    orch = Orch()
    orch.run_world_sim = sim
    sd2 = ROOT / "saves" / "_suite_plot_seed"
    if sd2.exists():
        shutil.rmtree(sd2, onerror=lambda f, p_, _: (Path(p_).chmod(0o700), f(p_)))
    orch.run_genesis_stage("seed", {"pack_id": PACK, "world_name": "_suite_plot_seed", "setup_answers": {}})
    assert "剧情规划" in seen["req"] and "前三章" in seen["req"]
    assert (sd2 / "world/design/剧情规划.md").exists() and (sd2 / "world/design/排期总表.md").exists()
    shutil.rmtree(sd2, onerror=lambda f, p_, _: (Path(p_).chmod(0o700), f(p_)))



@case("游戏中切换思考强度：只改这一项，其他台前设置不动，下一轮就用新的")
def _():
    import web.server as srv
    from engine.runtime.api_driver import front_driver_for_turn

    original = common.CONFIG_PATH.read_bytes()
    try:
        cfg = yaml.safe_load(original.decode("utf-8"))
        cfg["front_driver"] = {"type": "cli", "cli_tool": "claude", "cli_args": ["--model", "opus"], "api_key": "sk-保留"}
        common.CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        srv.handle_update_config({"front_driver": {"cli_effort": "high"}})
        saved = common.load_config()["front_driver"]
        assert saved["cli_effort"] == "high" and saved["cli_tool"] == "claude" and saved["api_key"] == "sk-保留", saved
        assert front_driver_for_turn(common.load_config())["cli_args"] == ["--model", "opus", "--effort", "high"]
    finally:
        common.CONFIG_PATH.write_bytes(original)


@case("Claude Code 启动参数：只带需要的工具，关掉用不到的固定开销，系统提示走文件")
def _():
    captured = {}
    saved = (cd.check_cli_available, cd.subprocess.Popen)

    class Done(Exception):
        pass

    def popen(cmd, **_kw):
        captured["cmd"] = cmd
        raise Done()

    cd.check_cli_available = lambda *_a: (True, "claude.exe")
    cd.subprocess.Popen = popen
    try:
        try:
            cd.run_cli_turn({"cli_tool": "claude", "cli_args": ["--model", "opus"]}, "动态内容", role="front", stream=True, on_delta=lambda _t: None,
                            system_prompt="静态内容", tools="Read,Grep,Glob", replace_system=True)
        except Done:
            pass
    finally:
        cd.check_cli_available, cd.subprocess.Popen = saved
    cmd = captured["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob" and cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--include-partial-messages"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--setting-sources") + 1] == "" and cmd[cmd.index("--model") + 1] == "opus"
    system_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
    assert system_file.read_text(encoding="utf-8") == "静态内容"
    assert "静态内容" not in " ".join(cmd), "系统提示太长，不能走命令行参数"


# ---------------- 不只是 Claude：API 驱动走同一套 ----------------
from engine.runtime import api_driver as ad  # noqa: E402


class ScriptedApi:
    """替换 run_openai_compat_loop：模拟 DeepSeek 这类 OpenAI 兼容接口。"""

    def __init__(self, *replies, plan_ready=True, back=None):
        self.replies = list(replies)
        self.calls = []
        self.plan_ready = plan_ready
        self.back = back or {"inherit_front": True}

    def __enter__(self):
        self.saved = (om.run_openai_compat_loop, om.load_config, om.plot.needs_planning)
        if self.plan_ready:
            om.plot.needs_planning = lambda _sd: False
        cfg = dict(om.load_config())
        cfg["front_driver"] = {"type": "api", "api_base": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro", "api_key": "sk-test"}
        cfg["back_driver"] = self.back
        om.load_config = lambda: cfg

        def fake(driver, system_prompt, messages, sd, role="front", **kw):
            self.calls.append({"driver": driver, "system": system_prompt, "messages": messages, "role": role, **kw})
            reply = self.replies.pop(0)
            if callable(reply):
                reply = reply()
            if kw.get("stream") and kw.get("on_delta"):
                for i in range(0, len(reply), 7):
                    kw["on_delta"](reply[i:i + 7])
            return {"text": reply, "tool_logs": [], "model": "fake"}

        om.run_openai_compat_loop = fake
        return self

    def __exit__(self, *_):
        om.run_openai_compat_loop, om.load_config, om.plot.needs_planning = self.saved


@case("API 驱动的普通回合：一次写完，只给查资料的工具，变更块写入存档、不给玩家看")
def _():
    sd = fresh()
    ops = json.dumps([{"op": "set", "file": "world/now.md", "key": "ui_mood", "value": "rest"}], ensure_ascii=False)
    seen = []
    with ScriptedApi(story("你推门进去。", ops)) as api:
        res = Orch()._run_turn_core("推门", save_name=SAVE, stream=True, on_delta=seen.append)
    assert len(api.calls) == 1 and api.calls[0]["role"] == "front"
    call = api.calls[0]
    assert [t["function"]["name"] for t in call["tools"]] == ["read_save_file"], "不再靠工具回写和开推演"
    assert "<state_changes>" in call["system"] and "apply_changes" not in call["system"]
    assert call["driver"]["effort"] == "low", call["driver"].get("effort")
    assert res["story"] == "你推门进去。" and "".join(seen) == "你推门进去。"
    meta, _ = common.read_frontmatter(sd / "world/now.md")
    assert meta.get("ui_mood") == "rest"


@case("API 驱动也会在回合前补剧情规划，幕后用更高的思考强度和不依赖命令行的说明")
def _():
    sd = fresh()
    path = sd / "world/design/剧情规划.md"

    def plan():
        path.write_text(PLAN, encoding="utf-8")
        return "规划简报"

    with ScriptedApi(plan, story("第一轮。"), plan_ready=False) as api:
        Orch()._run_turn_core("行动一", save_name=SAVE)
    assert [c["role"] for c in api.calls] == ["back", "front"]
    back, front = api.calls
    assert "剧情规划不够了" in back["messages"][0]["content"]
    assert back["driver"]["effort"] == "high" and front["driver"]["effort"] == "low"
    assert "这里没有命令行" in back["system"] and '"op":"add_file"' in back["system"]
    assert back.get("max_steps", 0) >= 20
    assert "幕后刚结算完" in front["messages"][-1]["content"] and "规划简报" in front["messages"][-1]["content"]


@case("章节摘要在 API 驱动下也由模型来写，不给工具")
def _():
    sd = fresh()
    archive_many(sd, om.chapter_log.CHAPTER_TURNS + 3)  # 最近三轮原文不急着总结
    total = om.story_log.count_turns(sd)
    span = om.chapter_log.next_chapter_range(sd, total)
    assert span
    with ScriptedApi("# 冬月\n\n爹和你在铺子里过了一冬。") as api:
        Orch()._write_chapter(sd, span)
    assert api.calls and api.calls[0]["tools"] == [] and api.calls[0]["role"] == "summary"
    chapters = om.chapter_log.read_chapters(sd)
    assert "爹和你在铺子里过了一冬" in json.dumps(chapters, ensure_ascii=False), chapters


@case("思考强度：各家接口的参数写法，幕后默认更高，命令行参数不重复")
def _():
    assert ad.api_effort_fields("openai", "https://api.deepseek.com/v1", "deepseek-v4-pro", "medium") == {"reasoning_effort": "high"}
    assert ad.api_effort_fields("openai", "https://api.deepseek.com/v1", "deepseek-v4-pro", "max") == {"reasoning_effort": "max"}
    assert ad.api_effort_fields("openai", "https://api.openai.com/v1", "gpt-5", "max") == {"reasoning_effort": "high"}
    assert ad.api_effort_fields("anthropic", "https://api.anthropic.com/v1", "claude-opus-5-5", "low") == {"output_config": {"effort": "low"}}
    assert ad.api_effort_fields("openai", "x", "y", "auto") == {}
    api_cfg = {"front_driver": {"type": "api", "model": "deepseek-v4-pro", "cli_effort": "medium"}, "back_driver": {"inherit_front": True}}
    assert ad.front_driver_for_turn(api_cfg)["effort"] == "medium", "老配置的 cli_effort 照样认"
    assert ad.resolve_back_driver(api_cfg)["effort"] == "high"
    api_cfg["back_driver"]["effort"] = "max"
    assert ad.resolve_back_driver(api_cfg)["effort"] == "max"
    cli_cfg = {"front_driver": {"type": "cli", "cli_tool": "claude", "cli_args": ["--effort", "low"]}, "back_driver": {"inherit_front": True}}
    back = ad.resolve_back_driver(cli_cfg)
    assert back["cli_args"].count("--effort") == 1 and back["cli_args"][-1] == "high", back["cli_args"]
    other = {"front_driver": {"type": "cli", "cli_tool": "gemini", "cli_args": []}, "back_driver": {"inherit_front": True}}
    assert "--effort" not in ad.resolve_back_driver(other)["cli_args"], "别的命令行工具没有这个参数"


@case("思考强度：接口不认这个参数时去掉重发一次，之后不再带")
def _():
    sent = []
    saved = ad._post_json

    def post(url, headers, payload, timeout=150.0):
        sent.append(dict(payload))
        if "reasoning_effort" in payload:
            raise RuntimeError("API 请求失败 (400)：unknown parameter reasoning_effort")
        return {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]}

    ad._post_json = post
    ad._NO_EFFORT_PARAMS.clear()
    try:
        driver = {"type": "api", "api_base": "http://127.0.0.1:9/v1", "model": "local-x", "effort": "high"}
        res = ad.run_openai_compat_loop(driver, "系统", [{"role": "user", "content": "你好"}], None, role="summary", tools=[])
        assert res["text"] == "好的" and len(sent) == 2 and "reasoning_effort" not in sent[1] and "tools" not in sent[1]
        ad.run_openai_compat_loop(driver, "系统", [{"role": "user", "content": "你好"}], None, role="summary", tools=[])
        assert len(sent) == 3 and "reasoning_effort" not in sent[2], "同一个模型不再试"
    finally:
        ad._post_json = saved
        ad._NO_EFFORT_PARAMS.clear()


@case("API 工具：写入工具带完整的操作格式，幕后读目录能列出文件")
def _():
    sd = fresh()
    desc = ad.FRONT_TOOLS_OPENAI[2]["function"]["description"]
    assert '"op":"replace","file"' in desc and "advance_days" in desc
    listing = ad.execute_tool(sd, "read_save_file", {"rel_path": "world/design"}, role="back")
    assert "world/design/剧情规划.md" in listing, listing


@case("剧情规划：模型换了标题写法也认得出（老存档没有模板时 DeepSeek 实际写出过这几种）")
def _():
    variants = {
        "接下来的章 / 进行中的章": "## 当前卷节拍\n\n走到起。\n\n## 已完成的章（一行一章）\n\n### 第一章 旧\n\n## 进行中的章\n\n### 第二章 甲\nx\n\n## 接下来的章\n\n### 第三章 乙\ny\n\n### 第四章 丙\nz\n",
        "每章一个二级标题": "## 当前卷与节拍\n\n当前卷：序章。\n\n## 已完成的章\n\n- 第一章\n\n## 第二章：甲（进行中）\nx\n\n## 第三章：乙\ny\n\n## 第四章：丙\nz\n",
        "模板写法": PLAN,
    }
    for name, text in variants.items():
        chapters = om.plot.upcoming(text)
        assert len(chapters) >= 3, (name, chapters)
        assert not any("旧" in c[0] or c[0].startswith("第一章") for c in chapters), (name, chapters)
    two = om.plot.upcoming(variants["接下来的章 / 进行中的章"])
    assert "进行中" in two[0][0]
    assert om.plot._volume(variants["每章一个二级标题"]) == "当前卷：序章。".replace("当前卷：", "")


@case("剧情规划：老存档缺骨架时，排规划前引擎先补上，已有的不覆盖")
def _():
    sd = fresh()
    folder = sd / "world/design"
    for name in ("剧情规划.md", "排期总表.md"):
        (folder / name).unlink()
    (folder / "07-排期总表.md").write_text("# 自己的排期", encoding="utf-8")
    added = om.plot.ensure_templates(sd)
    assert added == ["剧情规划.md"], added
    assert (folder / "07-排期总表.md").read_text(encoding="utf-8") == "# 自己的排期"
    back = ctxm.build_system_prompt(sd, role="back")
    assert "## 接下来几章" in back and "### 第N章 · 标题（状态：进行中" in back


@case("变更块漏了：当场补问一次只要变更块，补到了就照常写入")
def _():
    sd = fresh()
    ops = json.dumps([{"op": "set", "file": "world/now.md", "key": "ui_mood", "value": "rest"}], ensure_ascii=False)
    seen = []
    with ScriptedApi("你推门进去，过了两天。", f"<state_changes>\n{ops}\n</state_changes>") as api:
        res = Orch()._run_turn_core("推门", save_name=SAVE, stream=True, on_delta=seen.append)
    assert len(api.calls) == 2
    ask = api.calls[1]["messages"]
    assert [m["role"] for m in ask] == ["user", "assistant", "user"] and ask[1]["content"] == "你推门进去，过了两天。"
    assert api.calls[1]["tools"] == [] and "只输出这一轮的变更块" in ask[2]["content"]
    assert res["story"] == "你推门进去，过了两天。" and "".join(seen) == "你推门进去，过了两天。"
    assert common.read_frontmatter(sd / "world/now.md")[0].get("ui_mood") == "rest"
    assert not ctxm.read_pending_turn(sd).get("missing_changes")
    with ScriptedApi("又一轮。", "[]") as api:
        Orch()._run_turn_core("再推", save_name=SAVE)
    assert not ctxm.read_pending_turn(sd).get("missing_changes"), "只回了一个空数组也算交了"


@case("变更块：每轮输入最后都提醒附变更块；补问两次都没拿到就记下来，下一轮让模型补，补上后不再提")
def _():
    sd = fresh()
    with ScriptedApi("你推门进去，过了两天。", "好的。", "还是没有。", story("补上了。", "[]")) as api:
        result = Orch()._run_turn_core("推门", save_name=SAVE)
        assert result["archived"] and not result["state_synced"] and result["message"]
        assert len(api.calls) == 3, "补问落空时再问一次"
        first = api.calls[0]["messages"]
        assert len(first) == 1 and first[0]["role"] == "user", "最近几轮作为前文给出，不冒充模型自己的回答"
        assert first[0]["content"].rstrip().endswith("没有变化也要写 []。）")
        assert ctxm.read_pending_turn(sd).get("missing_changes")
        Orch()._run_turn_core("继续", save_name=SAVE)
        assert "漏了 <state_changes>" in api.calls[3]["messages"][0]["content"]
    assert not ctxm.read_pending_turn(sd).get("missing_changes")
    with ScriptedCli(story("正常一轮。")) as cli:
        Orch()._run_turn_core("再走", save_name=SAVE)
    assert cli.calls[0]["prompt"].rstrip().endswith("没有变化也要写 []。）")
    assert "漏了 <state_changes>" not in cli.calls[0]["prompt"]


sd_final = ROOT / "saves" / SAVE
if sd_final.exists():
    shutil.rmtree(sd_final, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
