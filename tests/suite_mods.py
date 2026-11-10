"""存档定制层 mods 与自定义面板的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import apply as ap  # noqa: E402
import common  # noqa: E402
import context as ctxm  # noqa: E402
import mods  # noqa: E402
import pack_io  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
PACK = "xianxia"
SAVE = "_suite_mods"
PANEL = "<div id='a'></div><script>RPG.onState(function(s){})</script>"
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


def fresh(pack=PACK):
    sd = ROOT / "saves" / SAVE
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", pack], capture_output=True, cwd=ROOT, check=True)
    return sd


def git(sd, *args):
    return subprocess.run(["git", "-C", str(sd), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


def pack_snapshot(pid=PACK):
    root = ROOT / "packs" / pid
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def expect_error(fn, needle):
    try:
        fn()
    except ValueError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"应当报错：{needle}")


# ---------------- 覆盖加载 ----------------
@case("覆盖：主题和状态栏合并，世界包不变")
def _():
    sd = fresh()
    before = pack_snapshot()
    mods.apply_ui_patch(sd, {"css_vars": {"--accent-primary": "#123456"}, "dialogue_box": {"max_width": "800px"}}, {"tagline": "我的一局"})
    theme = common.load_theme(sd, PACK)
    assert theme["css_vars"]["--accent-primary"] == "#123456"
    assert theme["css_vars"]["--bg-main"] == "#f1ebde", "未覆盖的值要保留"
    assert theme["dialogue_box"]["style"] == "parchment-scroll"
    assert common.load_ui_schema(sd, PACK)["tagline"] == "我的一局"
    assert common.load_theme(None, PACK)["css_vars"]["--accent-primary"] == "#9c3a2a", "其他存档不受影响"
    assert Orch().get_hud_state(SAVE)["theme"]["css_vars"]["--accent-primary"] == "#123456"
    assert pack_snapshot() == before
    assert git(sd, "log", "-1", "--format=%s").startswith("[tuner]")


@case("覆盖：文档和命令进入提示词，同名命令存档优先")
def _():
    sd = fresh()
    mods.write_files(sd, {
        "rules.md": "饥饿值规则：每天减一。",
        "backstage.md": "幕后专属补充。",
        "commands/yixiantian.md": "---\ndescription: 被存档改写\n---\n改写版：$ARGUMENTS",
        "commands/eat.md": "---\ndescription: 吃东西\narchive: true\n---\n吃：$ARGUMENTS",
    })
    front = ctxm.build_system_prompt(sd, role="front")
    back = ctxm.build_system_prompt(sd, role="back")
    assert "饥饿值规则" in front and "饥饿值规则" in back
    assert "幕后专属补充" in back and "幕后专属补充" not in front
    assert "/eat" in front and "被存档改写" in front
    orch = Orch()
    _cmd, expanded = orch.expand_slash_command("/yixiantian 跟他走", sd)
    assert expanded == "改写版：跟他走"
    assert orch._command_counts_as_turn(sd, "eat")
    assert not orch._pack_command_file(sd, "../../evil")


@case("校验：不合规的改动恢复原样")
def _():
    sd = fresh()
    mods.write_files(sd, {"rules.md": "原有规则"})
    head = git(sd, "rev-parse", "HEAD")
    expect_error(lambda: mods.write_files(sd, {"rules.md": "新规则", "data/a.json": "不是 JSON"}), "JSON")
    expect_error(lambda: mods.apply_ui_patch(sd, None, {"player_status": [{"key": "hp", "widget": "NOPE"}]}), "未知控件")
    expect_error(lambda: mods.apply_ui_patch(sd, {"dialogue_box": {"style": "NOPE"}}), "dialogue_box")
    expect_error(lambda: mods.write_files(sd, panels=[{"id": "ghost", "label": "没有文件"}]), "不存在")
    assert (sd / "mods/rules.md").read_text(encoding="utf-8").strip() == "原有规则"
    assert not (sd / "mods/data").exists()
    assert not (sd / "mods/pack.yaml").exists()
    assert git(sd, "rev-parse", "HEAD") == head


@case("校验：路径白名单挡住越界和服务器代码")
def _():
    sd = fresh()
    for bad in ("../world/canon.md", "panels/../../x.html", "panels/a.js", "hack.py", "commands/a.py", "world/now.md", "data/../../a.json"):
        expect_error(lambda p=bad: mods.write_files(sd, {p: "x"}), "不能写入")
    assert not (ROOT / "x.html").exists()


@case("回合：撤回回合时定制保留，回合内的面板数据一起撤销")
def _():
    sd = fresh()
    mods.write_files(sd, {"panels/map.html": PANEL, "data/map.json": '{"nodes": ["村"]}'}, [{"id": "map", "label": "地图"}])
    res = ap.apply_operations(sd, [{"op": "set_panel_data", "panel": "map", "data": {"nodes": ["村", "城"]}}], commit_checkpoint=False)
    assert res.get("ok", True) and not res.get("rolled_back"), res
    ap.archive_story_turn(sd, "去城里", "到了城里。")
    ap.git_turn_checkpoint(sd, "去城里", prefix="[turn]")
    assert mods.panel_data(sd, "map") == {"nodes": ["村", "城"]}
    mods.write_files(sd, {"panels/bag.html": PANEL}, [{"id": "bag", "label": "背包"}], label="回合后加的面板")
    orch = Orch()
    orch.undo_last_turn(save_name=SAVE)
    assert mods.panel_data(sd, "map") == {"nodes": ["村"]}, "回合内的数据要撤销"
    assert (sd / "mods/panels/bag.html").exists(), "回合后加的定制要保留"
    assert [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]][:1] == ["cultivation"]


@case("回合：面板数据拒绝非法 id")
def _():
    sd = fresh()
    res = ap.apply_operations(sd, [{"op": "set_panel_data", "panel": "../../x", "data": {}}], commit_checkpoint=False)
    assert res.get("rolled_back"), res
    assert not (sd.parent / "x.json").exists()


# ---------------- 面板 ----------------
@case("面板：世界包面板通过体检，缺文件和坏声明被拦下")
def _():
    rep = pack_io.doctor_pack(PACK)
    assert not rep["errors"] and not rep["warnings"], rep
    errors = []
    pack_io.check_panels([{"id": "a b", "label": "x"}, {"id": "a"}, {"id": "b", "label": "y", "file": "../x.html"}, {"id": "c", "label": "z"}], lambda rel: False, errors, [])
    assert len(errors) == 5, errors


@case("面板：存档可以新增、隐藏世界包面板，移除后恢复")
def _():
    sd = fresh()
    assert [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]] == ["cultivation"]
    mods.write_files(sd, {"panels/map.html": PANEL, "data/map.json": "{}"}, [{"id": "map", "label": "地图"}, {"id": "cultivation", "enabled": False}])
    assert [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]] == ["map"]
    assert Orch().get_panel_source("map", SAVE)["html"] == PANEL
    mods.remove_mod(sd, "panels/map.html")
    assert not (sd / "mods/data/map.json").exists()
    ids = [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]]
    assert ids == [], ids  # cultivation 仍被这一局隐藏
    mods.remove_mod(sd, "pack.yaml")
    assert [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]] == ["cultivation"]
    assert not mods.list_mods(sd)


@case("面板：只能取已声明的面板，不能读任意文件")
def _():
    sd = fresh()
    (sd / "mods/panels").mkdir(parents=True)
    (sd / "mods/panels/undeclared.html").write_text("x", encoding="utf-8")
    orch = Orch()
    for bad in ("undeclared", "../../world/canon", "..", "", None, "cultivation/../x"):
        try:
            orch.get_panel_source(bad, SAVE)
            raise AssertionError(f"应当拒绝：{bad}")
        except ValueError:
            pass
    assert "RPG.onState" in orch.get_panel_source("cultivation", SAVE)["html"]
    (sd / "mods/pack.yaml").write_text("ui_schema:\n  panels:\n    - {id: evil, label: x, file: ../../world/canon.md}\n", encoding="utf-8")
    try:
        orch.get_panel_source("evil", SAVE)
        raise AssertionError("应当拒绝越界 file")
    except ValueError:
        pass


@case("面板：拿到的数据不含隐藏内容和配置")
def _():
    sd = fresh()
    (sd / "world/secrets/机密.md").write_text("绝密阴谋内容", encoding="utf-8")
    sheet = sd / "player/sheet.md"
    sheet.write_text(sheet.read_text(encoding="utf-8").replace("zizhi: {根骨: 中, 悟性: 中}", "zizhi: {根骨: 上, 悟性: 绝世}", 1), encoding="utf-8")
    mods.write_files(sd, {"panels/map.html": PANEL, "data/map.json": '{"x": 1}'}, [{"id": "map", "label": "地图"}])
    state = Orch().get_hud_state(SAVE)["panel_state"]
    dumped = json.dumps(state, ensure_ascii=False)
    for hidden in ("至纯", "绝世", "绝密阴谋", "双胞弟弟", "api_key", "front_driver"):
        assert hidden not in dumped, hidden
    assert state["player"]["affinity"] == {"风": "寻常"}
    assert state["rules"]["ranks"][1] == "炼气"
    assert state["data"]["map"] == {"x": 1} and state["data"]["cultivation"] is None


# ---------------- 调教助手工具 ----------------
@case("调教工具：有存档时写 mods，世界包不变")
def _():
    sd = fresh()
    before = pack_snapshot()
    ctx = {"pack_id": PACK}
    out = ad.execute_tool(sd, "update_world_ui", {"theme_patch": {"css_vars": {"--accent-primary": "#111111"}}, "summary": "换色"}, role="tuner", tuner_context=ctx)
    assert "✓" in out and ctx["ui_updated"], out
    out = ad.execute_tool(sd, "update_narrative_style", {"style_rule_to_append": "台词再短一点", "note": "文风"}, role="tuner", tuner_context=ctx)
    assert "✓" in out, out
    out = ad.execute_tool(sd, "update_world_or_save", {"pack_doc_name": "setting.md", "pack_doc_append": "这一局多一座城。", "summary": "补设定"}, role="tuner", tuner_context=ctx)
    assert "✓" in out, out
    out = ad.execute_tool(sd, "write_mods", {"files": {"panels/map.html": PANEL}, "panels": [{"id": "map", "label": "地图", "gm_note": "到新地点时更新"}], "summary": "地图"}, role="tuner", tuner_context=ctx)
    assert "✓" in out, out
    assert pack_snapshot() == before
    assert "台词再短一点" in (sd / "mods/style_pack.md").read_text(encoding="utf-8")
    assert "这一局多一座城" in (sd / "mods/setting.md").read_text(encoding="utf-8")
    front = ctxm.build_system_prompt(sd, role="front")
    assert "到新地点时更新" in front and "set_panel_data" in front
    assert {m["path"] for m in mods.list_mods(sd)} >= {"theme.json", "pack.yaml", "panels/map.html"}
    out = ad.execute_tool(sd, "write_mods", {"files": {"../x.md": "x"}, "summary": "越界"}, role="tuner", tuner_context=ctx)
    assert "不能写入" in out, out


@case("调教工具：向导给缺界面的包补界面时写世界包")
def _():
    temp = ROOT / "packs" / "_suite_noui"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    try:
        (temp / "theme.json").unlink()
        ctx = {"pack_id": "_suite_noui", "write_scope": "pack"}
        sd = fresh()
        out = ad.execute_tool(sd, "update_world_ui", {"theme_patch": {"css_vars": {"--accent-primary": "#222222"}}}, role="tuner", tuner_context=ctx)
        assert "✓" in out, out
        assert (temp / "theme.json").exists() and not (sd / "mods/theme.json").exists()
    finally:
        shutil.rmtree(temp)


@case("调教工具：记录引擎做不到的需求")
def _():
    if mods.REQUESTS_LOG.exists():
        mods.REQUESTS_LOG.unlink()
    sd = fresh()
    out = ad.execute_tool(sd, "request_engine_extension", {"summary": "定时自动推进", "detail": "玩家想离线时世界也推进"}, role="tuner", tuner_context={"pack_id": PACK})
    assert "已记录" in out
    row = json.loads(mods.REQUESTS_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert row["summary"] == "定时自动推进" and row["save"] == SAVE


@case("调教：外部 CLI 写坏 mods 时恢复原样，写对时提交快照")
def _():
    sd = fresh()
    mods.write_files(sd, {"rules.md": "原有"})
    orch = Orch()
    cfg = om.load_config()
    orig_load, orig_cli = om.load_config, om.run_cli_turn
    om.load_config = lambda: {**cfg, "front_driver": {"type": "cli", "cli_tool": "claude"}}

    def bad_cli(*_a, **_k):
        (sd / "mods/rules.md").write_text("被改坏", encoding="utf-8")
        (sd / "mods/hack.py").write_text("x", encoding="utf-8")
        return {"text": "改好了"}

    def good_cli(*_a, **_k):
        (sd / "mods/commands").mkdir(exist_ok=True)
        (sd / "mods/commands/eat.md").write_text("吃：$ARGUMENTS", encoding="utf-8")
        return {"text": "改好了"}

    try:
        om.run_cli_turn = bad_cli
        res = orch._run_tuner_core("改一下", save_name=SAVE)
        assert "恢复原样" in res["reply"], res["reply"]
        assert (sd / "mods/rules.md").read_text(encoding="utf-8").strip() == "原有" and not (sd / "mods/hack.py").exists()
        om.run_cli_turn = good_cli
        orch._run_tuner_core("改一下", save_name=SAVE)
        assert git(sd, "log", "-1", "--format=%s").startswith("[tuner]")
        assert not git(sd, "status", "--porcelain")
    finally:
        om.load_config, om.run_cli_turn = orig_load, orig_cli


# ---------------- 热插拔 ----------------
@case("热插拔：删除整个 mods 恢复世界包原样")
def _():
    sd = fresh()
    mods.apply_ui_patch(sd, {"css_vars": {"--accent-primary": "#333333"}})
    mods.write_files(sd, {"rules.md": "新规则", "panels/a.html": PANEL}, [{"id": "a", "label": "甲"}])
    shutil.rmtree(sd / "mods")
    assert common.load_theme(sd, PACK)["css_vars"]["--accent-primary"] == "#9c3a2a"
    assert [p["id"] for p in common.load_ui_schema(sd, PACK)["panels"]] == ["cultivation"]
    assert "新规则" not in ctxm.build_system_prompt(sd, role="front")
    assert Orch().get_hud_state(SAVE)["mods"] == []


@case("世界包：面板可以被 AI 写入和导出，世界种子带上 mods")
def _():
    pid = "suite_panelpack"
    target = ROOT / "packs" / pid
    shutil.copytree(ROOT / "packs" / PACK, target)
    try:
        ad.execute_tool(None, "write_pack_bundle", {"pack_id": pid, "files": {"panels/x.html": PANEL, "panels/y.py": "x"}}, role="genesis", tuner_context={})
        assert (target / "panels/x.html").exists() and not (target / "panels/y.py").exists()
    finally:
        shutil.rmtree(target)
    sd = fresh()
    mods.write_files(sd, {"rules.md": "新规则"})
    import zipfile
    out = pack_io.export_seed(sd)
    with zipfile.ZipFile(out) as zf:
        assert any(n.endswith("mods/rules.md") for n in zf.namelist()), zf.namelist()[:5]
    out.unlink()


@case("命令行：list、check、remove 可用")
def _():
    sd = fresh()
    mods.write_files(sd, {"rules.md": "规则"})

    def run(*args):
        return subprocess.run([sys.executable, "engine/tools/mods.py", "--save", SAVE, *args], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)

    assert "mods/rules.md" in run("list").stdout
    assert run("check").returncode == 0
    (sd / "mods/data").mkdir()
    (sd / "mods/data/a.json").write_text("坏", encoding="utf-8")
    assert run("check").returncode == 1
    (sd / "mods/data/a.json").unlink()
    assert run("remove", "rules.md").returncode == 0
    assert not (sd / "mods/rules.md").exists()


for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
