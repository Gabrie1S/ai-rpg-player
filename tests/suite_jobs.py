"""长任务、取消与中断恢复的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import inspect
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import common  # noqa: E402
import mods  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import cli_driver as cd  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
PACK = "xianxia"
SAVE = "_suite_jobs"
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


def clear():
    sd = ROOT / "saves" / SAVE
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    return sd


def payload(**extra):
    return {"pack_id": PACK, "world_name": SAVE, **extra}

def seed_start(sd):
    path = sd / "world/now.md"
    meta, body = common.read_frontmatter(path)
    common.write_frontmatter(path, {**meta, "location": "loc_start"}, body)
    common.write_frontmatter(sd / "world/locations/loc_start.md",
                             {"id": "loc_start", "name": "起始地点", "detail": "full"}, "地点正文")


# ---------------- CLI 超时与取消 ----------------
class FakeCli:
    """用真实子进程模拟一个跑得很慢的 AI 工具。"""

    def __init__(self, code):
        self.code = code

    def __enter__(self):
        self.saved = (cd._build_cli_invocation, cd.check_cli_available, cd.HEARTBEAT_SECONDS, dict(cd.CLI_MAX_SECONDS))
        cd._build_cli_invocation = lambda *_a: ([sys.executable, "-c", self.code], None, None)
        cd.check_cli_available = lambda *_a: (True, "fake")
        cd.HEARTBEAT_SECONDS = 0.2

    def __exit__(self, *_):
        cd._build_cli_invocation, cd.check_cli_available, cd.HEARTBEAT_SECONDS, limits = self.saved
        cd.CLI_MAX_SECONDS.clear()
        cd.CLI_MAX_SECONDS.update(limits)


@case("CLI：长任务不再有 5 分钟硬超时，上限足够大")
def _():
    assert cd.CLI_MAX_SECONDS["back"] >= 1800 and cd.CLI_MAX_SECONDS["genesis"] >= 1800
    source = inspect.getsource(cd.run_cli_turn)
    assert "timeout=300" not in source


@case("CLI：运行中持续发状态，正常返回结果")
def _():
    beats = []
    with FakeCli("import time;time.sleep(1);print('写好了')"):
        res = cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="back", on_status=beats.append)
    assert res["text"].strip() == "写好了", res
    assert len(beats) >= 3, len(beats)


@case("CLI：玩家取消时很快结束，子进程不留下")
def _():
    calls = []

    def cancel_soon(_event):
        calls.append(1)
        if len(calls) > 2:
            raise om.StreamCancelled("取消")

    started = time.time()
    with FakeCli("import time;time.sleep(30)"):
        try:
            cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="back", on_status=cancel_soon)
            raise AssertionError("应当被取消")
        except om.StreamCancelled:
            pass
    assert time.time() - started < 10, time.time() - started


@case("CLI：流式模式没有输出时也能取消")
def _():
    calls = []

    def cancel_soon(_event):
        calls.append(1)
        if len(calls) > 2:
            raise om.StreamCancelled("取消")

    started = time.time()
    with FakeCli("import time;time.sleep(30)"):
        try:
            cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="front", stream=True, on_status=cancel_soon, on_delta=lambda _t: None)
            raise AssertionError("应当被取消")
        except om.StreamCancelled:
            pass
    assert time.time() - started < 10


@case("CLI：卡死超过上限时结束并提示")
def _():
    with FakeCli("import time;time.sleep(30)"):
        cd.CLI_MAX_SECONDS["back"] = 1
        started = time.time()
        try:
            cd.run_cli_turn({"cli_tool": "fake"}, "提示", role="back")
            raise AssertionError("应当超时")
        except RuntimeError as exc:
            assert "超过" in str(exc)
    assert time.time() - started < 10


# ---------------- 同一时间只跑一个 ----------------
@case("长任务：进行中不能开始第二个长任务、回合或调教")
def _():
    clear()
    orch = Orch()
    started, release = threading.Event(), threading.Event()

    def slow_sim(sd, *_a, **_k):
        started.set()
        release.wait(20)
        seed_start(sd)
        return "简报"

    orch.run_world_sim = slow_sim
    worker = threading.Thread(target=lambda: orch.run_genesis_stage("seed", payload()), daemon=True)
    worker.start()
    assert started.wait(10)
    try:
        for call in (
            lambda: orch.run_genesis_stage("customize_ui", {"pack_id": PACK}),
            lambda: orch._run_turn_core("行动", save_name=SAVE),
            lambda: orch._run_tuner_core("改颜色", save_name=SAVE),
            lambda: orch.undo_last_turn(save_name=SAVE),
        ):
            try:
                call()
                raise AssertionError("应当拒绝")
            except ValueError as exc:
                assert "生成世界" in str(exc), exc
        state = orch.get_job_state()["job"]
        assert state["label"] == "生成世界" and "factions" in state["counts"], state
        assert orch.get_hud_state(SAVE)["seed_state"] == "running"
    finally:
        release.set()
        worker.join(20)
    assert orch.get_job_state()["job"] is None
    assert orch.get_job_state()["last"]["ok"] is True


# ---------------- 关闭页面、取消、中断 ----------------
@case("关闭页面：生成继续跑完")
def _():
    sd = clear()
    orch = Orch()
    gate, done = threading.Event(), threading.Event()

    def slow_sim(_sd, _args, _cfg, on_status=None):
        gate.wait(20)
        on_status({"type": "status", "label": "继续"})
        seed_start(_sd)
        done.set()
        return "简报"

    orch.run_world_sim = slow_sim
    stream = orch.stream_genesis_stage("seed", payload())
    next(stream)
    stream.close()  # 浏览器断开连接
    gate.set()
    assert done.wait(20), "生成被断开连接打断了"
    deadline = time.time() + 20
    while orch.get_job_state()["job"] and time.time() < deadline:
        time.sleep(0.1)
    assert orch.get_job_state()["last"]["ok"] is True, orch.get_job_state()["last"]
    assert not (sd / "world/seed_status.yaml").exists()


@case("取消：任务结束，存档标记为没生成完，可以接着生成")
def _():
    sd = clear()
    orch = Orch()

    def looping_sim(_sd, _args, _cfg, on_status=None):
        for _ in range(200):
            on_status({"type": "status", "label": "继续"})
            time.sleep(0.05)
        return "简报"

    orch.run_world_sim = looping_sim
    events = []
    for ev in orch.stream_genesis_stage("seed", payload(setup_answers={"name": "乘风"})):
        events.append(ev)
        if len(events) == 3:
            orch.cancel_active_streams()
    assert events[-1].get("cancelled"), events[-1]
    last = orch.get_job_state()["last"]
    assert last["cancelled"] and not orch.get_job_state()["job"]
    hud = orch.get_hud_state(SAVE)
    assert hud["seed_state"] == "interrupted" and hud["seed_error"], hud["seed_state"]
    assert common.read_frontmatter(sd / "player/sheet.md")[0]["name"] == "乘风"


@case("关掉启动窗口：下次打开提示没生成完，接着生成时保留已有内容")
def _():
    sd = clear()
    orch = Orch()
    orch.run_world_sim = lambda *_a, **_k: "简报"
    # 第一次生成写到一半进程被杀：标记留在 generating，内存里的任务状态消失
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", PACK], capture_output=True, cwd=ROOT, check=True)
    (sd / "world/setup.yaml").write_text(yaml.safe_dump({"name": "乘风"}, allow_unicode=True), encoding="utf-8")
    (sd / "world/seed_status.yaml").write_text("state: generating\n", encoding="utf-8")
    (sd / "world/factions/fac_half.md").write_text("写到一半", encoding="utf-8")
    mods.write_files(sd, {"rules.md": "玩家规则"})

    fresh_orch = Orch()  # 模拟重新启动
    hud = fresh_orch.get_hud_state(SAVE)
    assert hud["seed_state"] == "interrupted", hud["seed_state"]

    seen = {}

    def resume_sim(_sd, args, _cfg, on_status=None):
        seen["req"] = args["request"]
        seed_start(_sd)
        return "简报"

    fresh_orch.run_world_sim = resume_sim
    (sd / "world/factions/fac_half.md").unlink()  # 校验要求格式合规，这里只验证继续生成的流程
    res = fresh_orch.run_genesis_stage("seed", payload(setup_answers={}))
    assert res["ok"], res["validation_errors"]
    assert "上次没生成完" in seen["req"] and "主角的名字：乘风" in seen["req"]
    assert common.read_frontmatter(sd / "player/sheet.md")[0]["name"] == "乘风"
    assert (sd / "mods/rules.md").exists()
    assert not (sd / "world/seed_status.yaml").exists()
    assert fresh_orch.get_hud_state(SAVE)["seed_state"] == ""


@case("生成失败：错误保留下来，界面能看到")
def _():
    clear()
    orch = Orch()

    def boom(*_a, **_k):
        raise RuntimeError("AI 工具出错了")

    orch.run_world_sim = boom
    try:
        orch.run_genesis_stage("seed", payload())
        raise AssertionError("应当报错")
    except RuntimeError:
        pass
    last = orch.get_job_state()["last"]
    assert not last["ok"] and "出错" in last["error"] and last["label"] == "生成世界"
    hud = orch.get_hud_state(SAVE)
    assert hud["seed_state"] == "interrupted" and "出错" in hud["seed_error"]


@case("标记文件：不进存档快照，撤回和回滚不会清掉它")
def _():
    sd = clear()
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", PACK], capture_output=True, cwd=ROOT, check=True)
    (sd / "world/seed_status.yaml").write_text("state: generating\n", encoding="utf-8")
    status = subprocess.run(["git", "-C", str(sd), "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8").stdout
    assert "seed_status" not in status, status


# ---------------- 重新生成：旧世界不动，生成完由玩家决定 ----------------
def git_lines(sd, *args):
    return subprocess.run(["git", "-C", str(sd), *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def fingerprint(sd):
    """旧存档的全部内容（不含 .git）和提交位置。"""
    files = {
        str(p.relative_to(sd)): p.read_bytes()
        for p in sorted(Path(sd).rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(sd).parts[:1]
    }
    return files, git_lines(sd, "rev-parse", "HEAD")


def writing_sim(text):
    def sim(sd, _args, _cfg, on_status=None):
        (Path(sd) / "world/lore.md").write_text(text, encoding="utf-8")
        seed_start(sd)
        return "简报"
    return sim


def seeded_world(orch=None):
    """生成完成的世界，带玩家定制和一处没提交的改动。"""
    sd = clear()
    clear_candidates()
    orch = orch or Orch()
    orch.run_world_sim = writing_sim("第一版")
    assert orch.run_genesis_stage("seed", payload(setup_answers={"name": "乘风"}))["ok"]
    mods.write_files(sd, {"rules.md": "玩家规则"})
    (sd / "log/probe.txt").write_text("没提交的改动", encoding="utf-8")
    return sd, orch


def clear_candidates():
    for d in (ROOT / "saves").iterdir():
        if d.is_dir() and d.name.startswith(SAVE) and d.name != SAVE:
            shutil.rmtree(d, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))


def candidate_dir():
    return ROOT / "saves" / f"{SAVE}（重新生成）"


@case("重新生成：旧世界一个字节不变，完成后等玩家决定，当前存档不切换")
def _():
    sd, orch = seeded_world()
    before = fingerprint(sd)
    seen = {}

    def sim(target, args, _cfg, on_status=None):
        seen["target"] = target.name
        seen["req"] = args["request"]
        seen["current"] = common.current_save_name()
        seen["job"] = orch.get_job_state()["job"]
        return writing_sim("第二版")(target, args, _cfg, on_status)

    orch.run_world_sim = sim
    res = orch.run_genesis_stage("seed", payload(setup_answers={}))
    assert res["ok"] and res["replaces"] == SAVE, res
    assert fingerprint(sd) == before, "旧存档被改动了"
    assert seen["target"] == candidate_dir().name and seen["current"] == SAVE
    assert seen["job"]["replaces"] == SAVE and seen["job"]["world"] == SAVE
    assert "全新的世界" in seen["req"] and "主角的名字：乘风" in seen["req"], seen["req"]
    hud = orch.get_hud_state()
    assert hud["save_name"] == SAVE
    assert candidate_dir().name not in [s["name"] for s in hud["saves"]]
    pending = hud["pending_worlds"]
    assert [p["state"] for p in pending] == ["ready"] and pending[0]["replaces"] == SAVE, pending
    # 新世界带着玩家的定制，世界名不用临时目录名
    assert (candidate_dir() / "mods/rules.md").exists()
    assert common.load_world(candidate_dir())["name"] == SAVE
    assert not (ROOT / "exports" / f"{candidate_dir().name}-seed.rpgpack").exists()
    last = orch.get_job_state()["last"]
    assert last["ok"] and last["replaces"] == SAVE and last["result"]["replaces"] == SAVE


@case("重新生成：结果没决定前不能再来一次")
def _():
    sd, orch = seeded_world()
    orch.run_genesis_stage("seed", payload(setup_answers={}))
    finished_at = orch.get_job_state()["last"]["finished_at"]
    try:
        orch.run_genesis_stage("seed", payload(setup_answers={}))
        raise AssertionError("应当拒绝")
    except ValueError as exc:
        assert "还没决定" in str(exc), exc
    assert orch.get_job_state()["last"]["finished_at"] == finished_at, "被拒绝不算一次任务"


@case("用新的：新世界接替原名，旧世界改名留作备份，内容和没提交的改动都还在")
def _():
    sd, orch = seeded_world()
    orch.run_world_sim = writing_sim("第二版")
    orch.run_genesis_stage("seed", payload(setup_answers={}))
    res = orch.resolve_pending_world(candidate_dir().name, "use_new")
    backup = ROOT / "saves" / res["backup"]
    assert res["save_name"] == SAVE and res["hud"]["save_name"] == SAVE and common.current_save_name() == SAVE
    assert (sd / "world/lore.md").read_text(encoding="utf-8") == "第二版"
    assert not (sd / "world/candidate.yaml").exists() and not candidate_dir().exists()
    assert (backup / "world/lore.md").read_text(encoding="utf-8") == "第一版"
    assert (backup / "log/probe.txt").exists()
    assert "probe.txt" not in git_lines(backup, "show", "--name-only", "--format=", "HEAD")
    assert common.load_world(backup)["name"] == res["backup"] and common.load_world(sd)["name"] == SAVE
    names = {s["name"] for s in res["hud"]["saves"]}
    assert {SAVE, res["backup"]} <= names and res["hud"]["pending_worlds"] == []
    assert (ROOT / "exports" / f"{SAVE}-seed.rpgpack").exists()
    assert "[seed]" in git_lines(sd, "log", "--format=%s")
    assert "candidate" not in git_lines(sd, "status", "--porcelain")


@case("保留旧的：只丢弃新生成的内容；都留下：新世界另存一局")
def _():
    sd, orch = seeded_world()
    before = fingerprint(sd)
    orch.run_world_sim = writing_sim("第二版")
    orch.run_genesis_stage("seed", payload(setup_answers={}))
    res = orch.resolve_pending_world(candidate_dir().name, "keep_old")
    assert not candidate_dir().exists() and res["hud"]["pending_worlds"] == []
    assert fingerprint(sd) == before and common.current_save_name() == SAVE

    orch.run_genesis_stage("seed", payload(setup_answers={}))
    res = orch.resolve_pending_world(candidate_dir().name, "keep_both")
    assert res["save_name"] == f"{SAVE}（第2版）" and common.current_save_name() == res["save_name"]
    both = ROOT / "saves" / res["save_name"]
    assert (both / "world/lore.md").read_text(encoding="utf-8") == "第二版"
    assert common.load_world(both)["name"] == res["save_name"]
    assert fingerprint(sd) == before
    assert not (both / "world/candidate.yaml").exists()


@case("重新生成中途取消：旧世界不变，可以继续生成或放弃，上次的想法还在")
def _():
    sd, orch = seeded_world()
    before = fingerprint(sd)

    def looping_sim(_sd, _args, _cfg, on_status=None):
        for _ in range(200):
            on_status({"type": "status", "label": "继续"})
            time.sleep(0.05)
        return "简报"

    orch.run_world_sim = looping_sim
    events = []
    for ev in orch.stream_genesis_stage("seed", payload(setup_answers={}, protagonist_pref="想当剑修")):
        events.append(ev)
        if len(events) == 3:
            assert orch.get_job_state()["job"]["phase"] == "继续"
            orch.cancel_active_streams()
    assert events[-1].get("cancelled"), events[-1]
    assert fingerprint(sd) == before
    hud = orch.get_hud_state()
    assert hud["seed_state"] == "" and hud["save_name"] == SAVE
    pending = hud["pending_worlds"]
    assert pending[0]["state"] == "interrupted" and pending[0]["error"], pending
    try:
        orch.resolve_pending_world(pending[0]["name"], "use_new")
        raise AssertionError("没生成完不能使用")
    except ValueError:
        pass

    seen = {}

    def resume_sim(target, args, _cfg, on_status=None):
        seen.update(target=target.name, req=args["request"])
        seed_start(target)
        return "简报"

    orch.run_world_sim = resume_sim
    res = orch.run_genesis_stage("seed", payload(setup_answers={}))
    assert res["ok"] and seen["target"] == candidate_dir().name
    assert "想当剑修" in seen["req"] and "上次没生成完" in seen["req"], seen["req"]
    assert fingerprint(sd) == before


@case("玩过的世界重新生成也走候选存档，回合记录不动")
def _():
    sd, orch = seeded_world()
    story = sd / "log/story/vol1"
    story.mkdir(parents=True, exist_ok=True)
    (story / "d1.md").write_text("## 第1天\n\n**玩家**：走\n\n走了。\n", encoding="utf-8")
    before = fingerprint(sd)
    orch.run_world_sim = writing_sim("第二版")
    assert orch.run_genesis_stage("seed", payload(setup_answers={}))["replaces"] == SAVE
    assert fingerprint(sd) == before


@case("同名不同世界：开始前就拒绝，不留文件、不算任务")
def _():
    sd, orch = seeded_world()
    before = fingerprint(sd)
    finished_at = orch.get_job_state()["last"]["finished_at"]
    called = []
    orch.run_world_sim = lambda *_a, **_k: called.append(1)
    try:
        orch.run_genesis_stage("seed", {"pack_id": "cyberpunk", "world_name": SAVE})
        raise AssertionError("应当拒绝")
    except ValueError as exc:
        assert "另一个世界" in str(exc), exc
    assert not called and not candidate_dir().exists() and fingerprint(sd) == before
    assert orch.get_job_state()["last"]["finished_at"] == finished_at and orch.get_job_state()["job"] is None


@case("空白存档：直接生成在里面，不需要玩家决定")
def _():
    sd = clear()
    clear_candidates()
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", PACK], capture_output=True, cwd=ROOT, check=True)
    orch = Orch()
    orch.run_world_sim = writing_sim("第一版")
    res = orch.run_genesis_stage("seed", payload(setup_answers={"name": "乘风"}))
    assert res["ok"] and not res["replaces"] and res["save_name"] == SAVE, res
    assert (sd / "world/lore.md").read_text(encoding="utf-8") == "第一版"
    assert orch.get_hud_state()["pending_worlds"] == []
    assert next(s for s in orch.get_hud_state()["saves"] if s["name"] == SAVE)["state"] == "world"


@case("存档名不能带路径")
def _():
    orch = Orch()
    for bad in ("..", "a/b", "..\\x"):
        try:
            orch.run_genesis_stage("seed", {"pack_id": PACK, "world_name": bad})
            raise AssertionError(f"应当拒绝：{bad}")
        except ValueError as exc:
            assert "不合法" in str(exc), exc
    try:
        orch.resolve_pending_world("..", "keep_old")
        raise AssertionError("应当拒绝")
    except ValueError:
        pass


@case("长任务进行中：改配置、删定制、润色、整理世界包都被拦下")
def _():
    sd, orch = seeded_world()
    orch._begin_job("seed", SAVE, world=SAVE)
    try:
        for call in (
            lambda: orch.remove_mod("rules.md", SAVE),
            lambda: orch.edit_latest_story("改", SAVE),
            lambda: orch.add_style_example("片段", True, "", SAVE),
            lambda: orch.clear_tuner_history(SAVE),
            lambda: orch.run_pack_flatten(PACK),
            lambda: orch.import_pack_upload({}),
            lambda: orch.verify_driver_and_save({}),
            lambda: orch.resolve_pending_world("x", "keep_old"),
        ):
            try:
                call()
                raise AssertionError("应当拒绝")
            except ValueError as exc:
                assert "还没做完" in str(exc), exc
    finally:
        orch._end_job()
    assert (sd / "mods/rules.md").exists()


@case("创世：只返回完成文案时不导出种子，也不宣布界面更新")
def _():
    sd = clear()
    bundle = ROOT / "exports" / f"{SAVE}-seed.rpgpack"
    bundle.unlink(missing_ok=True)
    orch = Orch()
    orch.run_world_sim = lambda *_a, **_k: "世界生成完成"
    res = orch.run_genesis_stage("seed", payload(setup_answers={}))
    assert not res["ok"] and not bundle.exists()
    assert (sd / "world/seed_status.yaml").is_file()
    assert orch.get_job_state()["last"]["ok"] is False

    from unittest.mock import patch
    for dtype, runner in (("api", "run_openai_compat_loop"), ("cli", "run_cli_turn"), ("harness", "run_harness_turn")):
        with patch.object(om, "load_config", return_value={"front_driver": {"type": dtype}}), \
             patch.object(om, runner, return_value={"text": "界面已更新"}):
            try:
                orch.run_genesis_stage("customize_ui", {"pack_id": PACK, "save_name": SAVE})
                raise AssertionError("只回文案不能算界面已更新")
            except ValueError as exc:
                assert "没有检测到有效的界面更新" in str(exc), exc
            assert orch.get_job_state()["last"]["ok"] is False

    pack = ROOT / "packs" / PACK
    theme_path = pack / "theme.json"
    theme_before = theme_path.read_bytes() if theme_path.exists() else None
    mods_before = mods.snapshot(sd)

    def write_theme(*_args, **kwargs):
        if kwargs["tuner_context"].get("write_scope") == "pack":
            theme_path.write_text('{"css_vars": {"--accent-primary": "#123456"}}', encoding="utf-8")
        else:
            mods.apply_ui_patch(sd, {"css_vars": {"--accent-primary": "#123456"}})
        return {"text": "界面已更新"}

    try:
        with patch.object(om, "load_config", return_value={"front_driver": {"type": "api"}}), \
             patch.object(om, "run_openai_compat_loop", side_effect=write_theme):
            updated = orch.run_genesis_stage("customize_ui", {"pack_id": PACK, "save_name": SAVE})
        assert updated["ok"] and updated["ui_updated"]
    finally:
        mods.restore_snapshot(sd, mods_before)
        if theme_before is None:
            theme_path.unlink(missing_ok=True)
        else:
            theme_path.write_bytes(theme_before)


clear()
clear_candidates()
for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
