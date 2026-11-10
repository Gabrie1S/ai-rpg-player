"""回滚、撤回、重演与切片的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import json
import inspect
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import apply as ap  # noqa: E402
import context as ctxm  # noqa: E402
import pack_io  # noqa: E402
import split_md  # noqa: E402
import timeline  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad, cli_driver as cd  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
SAVE = "_suite_timeline"
results = []


def case(name):
    def deco(fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:
            results.append((False, name, f"{type(exc).__name__}: {str(exc)[:400]}"))
        return fn
    return deco


def fresh():
    sd = ROOT / "saves" / SAVE
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", "cyberpunk"], capture_output=True, cwd=ROOT, check=True)
    return sd


def git(sd, *args):
    return subprocess.run(["git", "-C", str(sd), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


def subjects(sd):
    return git(sd, "log", "--format=%s").splitlines()


def play_turn(sd, action, story, marker=None):
    if marker:
        with open(sd / "world/now.md", "a", encoding="utf-8") as fh:
            fh.write(f"\n<!--{marker}-->")
    ap.archive_story_turn(sd, action, story)
    ap.git_turn_checkpoint(sd, action, prefix="[turn]")


def scripted_loop(writer=None, text="这一轮的正文。"):
    def loop(*_a, **_k):
        if writer:
            writer()
        return {"text": text, "model": "scripted"}
    return loop


def with_loop(loop):
    class Ctx:
        def __enter__(self):
            # 剧情规划够用，这里只测台前这一次调用
            self.orig = (om.run_openai_compat_loop, om.plot.needs_planning)
            om.run_openai_compat_loop = loop
            om.plot.needs_planning = lambda _sd: False

        def __exit__(self, *_):
            om.run_openai_compat_loop, om.plot.needs_planning = self.orig
    return Ctx()


# ---------------- 回合失败与回滚 ----------------
@case("回合失败：已跟踪文件还原，新建文件清除")
def _():
    sd = fresh()

    def boom(*_a, **_k):
        (sd / "world/now.md").write_text("DIRTY", encoding="utf-8")
        (sd / "world/npcs/npc_orphan.md").write_text("---\nid: npc_orphan\n---\n", encoding="utf-8")
        raise RuntimeError("502")

    with with_loop(boom):
        try:
            Orch()._run_turn_core("测试", save_name=SAVE)
            raise AssertionError("应当抛出异常")
        except RuntimeError:
            pass
    assert "DIRTY" not in (sd / "world/now.md").read_text(encoding="utf-8")
    assert not (sd / "world/npcs/npc_orphan.md").exists()


@case("回合失败：不丢失回合前玩家未提交的改动")
def _():
    sd = fresh()
    (sd / "player/journal.md").write_text("玩家手改的日志", encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("502")

    with with_loop(boom):
        try:
            Orch()._run_turn_core("测试", save_name=SAVE)
        except RuntimeError:
            pass
    assert "玩家手改的日志" in (sd / "player/journal.md").read_text(encoding="utf-8")
    assert subjects(sd)[0].startswith("[sync]")


@case("回合失败：调教窗聊天记录不受影响（含旧存档迁移）")
def _():
    sd = fresh()
    (sd / ".gitignore").unlink()
    hist = sd / "log/tuner_history.json"
    hist.write_text("[]", encoding="utf-8")
    subprocess.run(["git", "-C", str(sd), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(sd), "commit", "-qm", "旧存档"], capture_output=True)
    hist.write_text('["聊天"]', encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("502")

    with with_loop(boom):
        try:
            Orch()._run_turn_core("测试", save_name=SAVE)
        except RuntimeError:
            pass
    assert "聊天" in hist.read_text(encoding="utf-8")
    assert not git(sd, "ls-files", "log/tuner_history.json")


@case("模型没有返回正文：回合不生效并报错")
def _():
    sd = fresh()
    base = git(sd, "rev-parse", "HEAD")
    with with_loop(scripted_loop(lambda: (sd / "world/now.md").write_text("DIRTY", encoding="utf-8"), text="")):
        try:
            Orch()._run_turn_core("测试", save_name=SAVE)
            raise AssertionError("应当抛出异常")
        except ValueError:
            pass
    assert "DIRTY" not in (sd / "world/now.md").read_text(encoding="utf-8")
    assert git(sd, "rev-parse", "HEAD") == base


@case("一轮内的多次提交折叠成唯一 [turn]")
def _():
    sd = fresh()
    base = git(sd, "rev-parse", "HEAD")

    def agent():
        (sd / "world/lore.md").write_text("一", encoding="utf-8")
        ap.git_turn_checkpoint(sd, "agent 回写 1")
        ap.archive_story_turn(sd, "行动", "外部 Agent 写的正文。")
        ap.git_turn_checkpoint(sd, "agent 回写 2")
        (sd / "world/lore.md").write_text("二", encoding="utf-8")

    with with_loop(scripted_loop(agent)):
        Orch()._run_turn_core("行动", save_name=SAVE)
    assert git(sd, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert subjects(sd)[0].startswith("[turn]")
    assert not git(sd, "status", "--porcelain")
    assert len(om.parse_story_History(sd)) == 1, "不应重复归档"


# ---------------- 撤回与重演 ----------------
@case("重演：润色提交随回合撤销，不重复")
def _():
    sd = fresh()
    base = git(sd, "rev-parse", "HEAD")
    play_turn(sd, "行动一", "第一幕正文。", "turn1")
    orch = Orch()
    orch.edit_latest_story("第一幕润色后。", save_name=SAVE)
    assert subjects(sd)[0].startswith("[polish]")
    seen = {}
    orch._run_turn_core = lambda inp, **k: (seen.update(inp=inp) or {"ok": True})
    orch._swipe_core(save_name=SAVE)
    assert git(sd, "rev-parse", "HEAD") == base
    assert "turn1" not in (sd / "world/now.md").read_text(encoding="utf-8")
    assert seen["inp"] == "行动一"


@case("重演：回合后的调教/GM 修改保留，并进入重演上下文")
def _():
    sd = fresh()
    play_turn(sd, "行动一", "第一幕正文。", "turn1")
    (sd / "world/lore.md").write_text("TUNER_EDIT", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "加设定", prefix="[tuner]")
    (sd / "world/canon.md").write_text("GM_EDIT", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "/gm 改设定", prefix="[gm]")
    seen = {}
    orch = Orch()

    def rerun(_inp, **_k):
        seen["lore"] = (sd / "world/lore.md").read_text(encoding="utf-8")
        return {"ok": True}

    orch._run_turn_core = rerun
    res = orch._swipe_core(save_name=SAVE)
    assert seen["lore"] == "TUNER_EDIT"
    assert "GM_EDIT" in (sd / "world/canon.md").read_text(encoding="utf-8")
    assert "turn1" not in (sd / "world/now.md").read_text(encoding="utf-8")
    assert "2 条场外修改" in res["message"]


@case("撤回：与被撤回内容冲突的修改不合并，并提示玩家")
def _():
    sd = fresh()
    sheet = sd / "world/lore.md"
    sheet.write_text("原文\n", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "底稿", prefix="[sync]")
    sheet.write_text("回合改的\n", encoding="utf-8")
    play_turn(sd, "行动一", "正文。")
    sheet.write_text("调教改的\n", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "改同一处", prefix="[tuner]")
    res = Orch().undo_last_turn(save_name=SAVE)
    assert sheet.read_text(encoding="utf-8").strip() == "原文"
    assert "未能保留" in res["message"] and "改同一处" in res["message"]
    assert not git(sd, "status", "--porcelain"), "冲突后工作区必须是干净的"


@case("撤回：存档记录随回合撤销")
def _():
    sd = fresh()
    play_turn(sd, "行动一", "正文。")
    (sd / "log/summaries.md").write_text("写到了被撤回的回合", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "/save", prefix="[save]")
    Orch().undo_last_turn(save_name=SAVE)
    assert "被撤回" not in (sd / "log/summaries.md").read_text(encoding="utf-8")


@case("只撤回：可连续逐轮往回退，并返回上一轮的输入")
def _():
    sd = fresh()
    base = git(sd, "rev-parse", "HEAD")
    play_turn(sd, "行动一", "第一幕。", "t1")
    play_turn(sd, "行动二", "第二幕。", "t2")
    play_turn(sd, "行动三", "第三幕。", "t3")
    orch = Orch()
    res = orch.undo_last_turn(save_name=SAVE)
    assert res["last_input"] == "行动三" and res["reverted_only"]
    assert [t["input"] for t in om.parse_story_History(sd)] == ["行动一", "行动二"]
    now = (sd / "world/now.md").read_text(encoding="utf-8")
    assert "t2" in now and "t3" not in now
    orch.undo_last_turn(save_name=SAVE)
    orch.undo_last_turn(save_name=SAVE)
    assert git(sd, "rev-parse", "HEAD") == base
    try:
        orch.undo_last_turn(save_name=SAVE)
        raise AssertionError("没有回合时应当报错")
    except ValueError:
        pass


@case("重演失败：上一轮原样恢复，并报告错误")
def _():
    sd = fresh()
    play_turn(sd, "行动一", "第一幕正文。", "turn1")
    (sd / "world/lore.md").write_text("TUNER_EDIT", encoding="utf-8")
    ap.git_turn_checkpoint(sd, "加设定", prefix="[tuner]")
    head = git(sd, "rev-parse", "HEAD")

    def boom(*_a, **_k):
        (sd / "world/npcs/npc_orphan.md").write_text("x", encoding="utf-8")
        raise RuntimeError("502")

    with with_loop(boom):
        try:
            Orch()._swipe_core(save_name=SAVE)
            raise AssertionError("应当抛出异常")
        except RuntimeError as exc:
            assert "上一轮已原样恢复" in str(exc)
    assert git(sd, "rev-parse", "HEAD") == head
    assert not (sd / "world/npcs/npc_orphan.md").exists()
    assert om.parse_story_History(sd)[-1]["story"] == "第一幕正文。"


@case("重演开局序章：使用开局指令，而不是把占位标签当行动")
def _():
    sd = fresh()
    play_turn(sd, om.OPENING_TURN_LABEL, "序章正文。")
    seen = {}
    orch = Orch()
    orch._run_turn_core = lambda inp, **k: (seen.update(inp=inp) or {"ok": True})
    orch._swipe_core(save_name=SAVE)
    assert seen["inp"] == om.OPENING_RERUN_INPUT


# ---------------- 流式中止 ----------------
class SlowLLM(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i in range(400):
                chunk = {"choices": [{"delta": {"content": f"字{i}"}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            pass


def streaming(run):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg_path = ROOT / "config.yaml"
    original = cfg_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(original)
    cfg["front_driver"].update(type="api", provider="openai_compat", api_base=f"http://127.0.0.1:{srv.server_port}/v1", api_key="k", model="m")
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    try:
        return run()
    finally:
        cfg_path.write_text(original, encoding="utf-8")
        srv.shutdown()


def cancel_after_deltas(orch, events_iter):
    events, deltas = [], 0
    for ev in events_iter:
        events.append(ev)
        if ev.get("type") == "delta":
            deltas += 1
            if deltas == 5:
                orch.cancel_active_streams()
    return events


@case("停止生成：不归档，存档回到行动之前")
def _():
    sd = fresh()
    base = git(sd, "rev-parse", "HEAD")
    orch = Orch()
    events = streaming(lambda: cancel_after_deltas(orch, orch.stream_turn("测试停止", save_name=SAVE)))
    assert events[-1].get("cancelled"), events[-1]
    assert git(sd, "rev-parse", "HEAD") == base
    assert not om.parse_story_History(sd)


@case("停止重演：上一轮原样恢复")
def _():
    sd = fresh()
    play_turn(sd, "行动一", "第一幕正文。", "turn1")
    head = git(sd, "rev-parse", "HEAD")
    orch = Orch()
    events = streaming(lambda: cancel_after_deltas(orch, orch.stream_swipe_turn(save_name=SAVE)))
    assert events[-1].get("cancelled"), events[-1]
    assert git(sd, "rev-parse", "HEAD") == head
    assert om.parse_story_History(sd)[-1]["story"] == "第一幕正文。"


# ---------------- 切片 ----------------
LONG = "# 大文档\n\n总览\n\n" + "".join(f"## 章节{i}\n\n" + ("内容" * 7000) + "\n\n" for i in range(3))


@case("切片：单章过长时继续拆到不超过上限")
def _():
    sd = fresh()
    (sd / "world/design/大设定.md").write_text(LONG, encoding="utf-8")
    ap.sync_sliced_indexes(sd)
    files = [p for p in (sd / "world/design").glob("*.md") if p.name != "目录.md"]
    assert not (sd / "world/design/大设定.md").exists()
    assert len(files) >= 7, [p.name for p in files]
    assert all(len(p.read_text(encoding="utf-8")) <= split_md.MAX_SLICE_CHARS + 60 for p in files)


@case("切片：没有标题的超长文件也能拆")
def _():
    body = "\n\n".join(("段落" * 500) for _ in range(30))
    assert all(len(c) <= split_md.MAX_SLICE_CHARS for c in split_md.chunk_body(body))
    assert all(len(c) <= split_md.MAX_SLICE_CHARS for c in split_md.chunk_body("字" * 40000))


@case("切片：同文件夹两份大文档互不覆盖，内容不丢")
def _():
    sd = fresh()
    folder = sd / "world/secrets"
    (folder / "甲.md").write_text("# 甲\n\n甲的总览\n\n## 同名章\n\n" + "甲" * 13000, encoding="utf-8")
    (folder / "乙.md").write_text("# 乙\n\n乙的总览\n\n## 同名章\n\n" + "乙" * 13000, encoding="utf-8")
    ap.sync_sliced_indexes(sd)
    text = "".join(p.read_text(encoding="utf-8") for p in folder.glob("*.md") if p.name != "目录.md")
    assert "甲的总览" in text and "乙的总览" in text
    assert text.count("甲") >= 13000 and text.count("乙") >= 13000


@case("切片：带编号的大文件拆分后不留重复原件")
def _():
    sd = fresh()
    folder = sd / "world/design"
    (folder / "07-旧设定.md").write_text("# 旧\n\n## 甲\n\n" + "甲" * 7000 + "\n\n## 乙\n\n" + "乙" * 7000, encoding="utf-8")
    ap.sync_sliced_indexes(sd)
    assert not (folder / "07-旧设定.md").exists()
    count = sum(p.read_text(encoding="utf-8").count("乙") for p in folder.glob("*.md") if p.name != "目录.md")
    assert 7000 <= count <= 7001, count  # 正文加上一个标题，不应翻倍


@case("切片：世界包只注入总览和目录，缓存不进导出包，体检不误判")
def _():
    pack = ROOT / "packs" / "_suite_big"
    if pack.exists():
        shutil.rmtree(pack)
    shutil.copytree(ROOT / "packs/cyberpunk", pack)
    (pack / "setting.md").write_text(LONG, encoding="utf-8")
    injected = ctxm._ensure_pack_doc_sliced(pack, pack / "setting.md")
    try:
        assert len(injected) < len(LONG) and "分层切片" in injected
        assert (pack / ".slices/setting/目录.md").exists()
        report = pack_io.doctor_pack("_suite_big")
        assert report["status"] == "ready" and not report["errors"], report
        out = pack_io.export_pack("_suite_big", ROOT / "exports" / "_suite_big.rpgpack")
        with zipfile.ZipFile(out) as zf:
            assert not [n for n in zf.namelist() if ".slices" in n]
        out.unlink()
    finally:
        shutil.rmtree(pack)
    assert "packs/*/.slices/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


@case("切片：预装配带上目录索引与埋伏笔秘密，台前可读 secrets")
def _():
    sd = fresh()
    (sd / "world/secrets/小秘密.md").write_text("# 小秘密\n机密X\n", encoding="utf-8")
    stage = sd / "world/stage.md"
    heading = "## 用来埋伏笔的秘密"
    text = stage.read_text(encoding="utf-8")
    assert heading in text
    stage.write_text(text.replace(heading, f"{heading}\n- `world/secrets/小秘密.md`", 1), encoding="utf-8")
    ctx = ctxm.assemble_turn_context(sd, "四处看看", om.load_config())
    files = list(ctx["files"].keys())
    for needed in ("world/index.md", "world/design/目录.md", "world/secrets/目录.md", "world/secrets/小秘密.md"):
        assert needed in files, (needed, files)
    assert "机密X" in ad.execute_tool(sd, "read_save_file", {"rel_path": "world/secrets/小秘密.md"}, role="front")


# ---------------- 其他修复 ----------------
@case("主题：不等宽的像素行自动补齐")
def _():
    theme = pack_io.normalize_theme_data({"pixel_frame": {"sprites": [{"rows": ["ab", "abcd", "a"]}]}})
    assert {len(r) for r in theme["pixel_frame"]["sprites"][0]["rows"]} == {4}


@case("CLI：40KB 的 Prompt 不进命令行")
def _():
    prompt = "x" * 40000
    for tool in ("claude", "codex", "gemini", "agy", "opencode"):
        cmd, stdin, tmp = cd._build_cli_invocation(tool + ".exe", tool, prompt, [])
        assert sum(len(str(c)) for c in cmd) < 8200, tool
        assert stdin
        if tmp:
            Path(tmp).unlink()


@case("Anthropic：消息以 user 开头且角色交替")
def _():
    _sys, msgs = ad._convert_messages_to_anthropic([
        {"role": "system", "content": "S"},
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "v"},
    ])
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user" and all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles


shutil.rmtree(ROOT / "saves" / SAVE, ignore_errors=True, onerror=None)
for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
