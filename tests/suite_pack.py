"""世界包规范、内置仙侠包与幕后模型配置的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import inspect
import json
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import common  # noqa: E402
import context as ctxm  # noqa: E402
import pack_io  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad, cli_driver as cd  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
PACK = "xianxia"
SAVE = "_suite_pack"
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
    subprocess.run([sys.executable, "engine/tools/newsave.py", SAVE, "--pack", PACK], capture_output=True, cwd=ROOT, check=True)
    return sd


def with_loop(loop):
    class Ctx:
        def __enter__(self):
            self.orig = om.run_openai_compat_loop
            om.run_openai_compat_loop = loop

        def __exit__(self, *_):
            om.run_openai_compat_loop = self.orig
    return Ctx()


# ---------------- 世界包结构 ----------------
@case("内置包通过体检，没有错误和警告")
def _():
    for pid in (PACK, "cyberpunk"):
        rep = pack_io.doctor_pack(pid)
        assert rep["status"] == "ready" and not rep["errors"], (pid, rep["errors"])
        assert not rep["warnings"], (pid, rep["warnings"])


@case("新建存档：模板覆盖生效，校验通过")
def _():
    sd = fresh()
    assert "风雷双灵根" in (sd / "world/canon.md").read_text(encoding="utf-8")
    assert (sd / "world/secrets/05-养父线索清单.md").exists()
    meta, _ = common.read_frontmatter(sd / "player/sheet.md")
    assert meta["realm"] == "凡人" and meta["linggen"]["雷"] == "至纯"
    assert "当前目标" in (sd / "player/journal.md").read_text(encoding="utf-8")
    from validate import validate
    rep, _ = validate(sd)
    assert not rep.errors, rep.errors


@case("历法：自定义月份名和日期名")
def _():
    sd = fresh()
    world, _ = common.safe_load_yaml(sd / "world/world.yaml")
    assert common.day_to_date(0, world["calendar"]) == "天元3726年冬月初一"
    assert common.day_to_date(2, world["calendar"]) == "天元3726年冬月初三"
    assert common.day_to_date(31, world["calendar"]) == "天元3726年腊月初二"
    assert common.day_to_date(0, {"era": "NC.", "start_year": 2084}) == "NC.2084年01月01日"


@case("历法：世界早于主角存在，内置世界包不从 1 年开始，写 1 时体检提醒")
def _():
    for pid in ("xianxia", "cyberpunk"):
        rep = pack_io.doctor_pack(pid)
        assert not [w for w in rep["warnings"] if "start_year" in w], rep["warnings"]
    temp = ROOT / "packs" / "_suite_year1"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    try:
        meta = yaml.safe_load((temp / "pack.yaml").read_text(encoding="utf-8"))
        meta["calendar"]["start_year"] = 1
        (temp / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
        rep = pack_io.doctor_pack("_suite_year1")
        assert [w for w in rep["warnings"] if "start_year" in w] and rep["status"] == "ready", rep
    finally:
        shutil.rmtree(temp)


@case("文风：台词规则进每轮提示词，前后端默认一致，没改过的旧配置自动更新，改过的不动")
def _():
    sd = fresh()
    assert "金句" in common.DEFAULT_NARRATIVE_PROMPT
    # 人物卡的台词和开场剧本是幕后写的，只给台前等于没给
    for role in ("front", "back", "genesis"):
        prompt = ctxm.build_system_prompt(sd, role=role)
        assert "不写金句" in prompt and "范例只校准文笔" in prompt, role
    assert "世界不等玩家" in ctxm.build_system_prompt(sd, role="back")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    body = js.split("const DEFAULT_UNIVERSAL_PROMPT =", 1)[1].split(";", 1)[0]
    js_text = "".join(re.findall(r'"([^"]*)"', body))
    assert js_text == common.DEFAULT_NARRATIVE_PROMPT, "前后端的默认提示词不一致"

    original = common.CONFIG_PATH.read_bytes()
    try:
        cfg = yaml.safe_load(original.decode("utf-8"))
        cfg["narrative_prompt"] = {"authors_note_template": next(iter(common.RETIRED_NARRATIVE_PROMPTS))}
        common.CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        assert common.load_config()["narrative_prompt"]["authors_note_template"] == common.DEFAULT_NARRATIVE_PROMPT
        assert "金句" in ctxm.build_authors_note(sd, common.load_config())
        cfg["narrative_prompt"] = {"authors_note_template": "玩家自己写的"}
        common.CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        assert common.load_config()["narrative_prompt"]["authors_note_template"] == "玩家自己写的"
    finally:
        common.CONFIG_PATH.write_bytes(original)


@case("界面：只显示世人所见的灵根和资质")
def _():
    sd = fresh()
    sheet = sd / "player/sheet.md"
    text = sheet.read_text(encoding="utf-8").replace("zizhi: {根骨: 中, 悟性: 中}", "zizhi: {根骨: 上, 悟性: 绝世}", 1)
    sheet.write_text(text, encoding="utf-8")
    hud = Orch().get_hud_state(SAVE)
    player = hud["player"]
    assert player["affinity"] == {"风": "寻常"}, player["affinity"]
    assert player["aptitude"] == {"根骨": "中", "悟性": "中"}, player["aptitude"]
    dumped = json.dumps(player, ensure_ascii=False)
    assert "至纯" not in dumped and "绝世" not in dumped, dumped[:300]
    assert any(i.get("label") == "灵根" and "风" in str(i.get("value")) for i in player["status_items"])
    assert hud["ui_schema"]["quick_commands"][0]["command"] == "/yixiantian"


@case("隐藏内容：世界包中心看不到幕后规则和亲定事实")
def _():
    detail = pack_io.inspect_pack_detail(PACK)
    assert "backstage.md" not in detail["docs"]
    shown = "".join(detail["docs"].values())
    for hidden in ("双胞弟弟", "风雷双灵根", "灵根为何被掩盖"):
        assert hidden not in shown, hidden


@case("提示词：幕后读 backstage，台前不读但知道亲定事实和命令")
def _():
    sd = fresh()
    front = ctxm.build_system_prompt(sd, role="front")
    back = ctxm.build_system_prompt(sd, role="back")
    assert "幕后规则" in back and "灵根为何被掩盖" in back
    assert "灵根为何被掩盖" not in front
    assert "/yixiantian" in front and "因果" in front
    ctx = ctxm.assemble_turn_context(sd, "四处看看", om.load_config())
    assert "world/canon.md" in ctx["files"]
    assert "双胞弟弟" in ctxm.format_context_markdown(ctx)


@case("导出再导入：命令和模板都带得上，体检通过")
def _():
    out = pack_io.export_pack(PACK, ROOT / "exports" / "_suite.rpgpack")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "pack/commands/yixiantian.md" in names
    assert "pack/templates/world/world/canon.md" in names
    target = ROOT / "packs" / "_suite_import"
    with zipfile.ZipFile(out) as zf:
        target.mkdir()
        for n in names:
            if n.startswith("pack/") and not n.endswith("/"):
                dest = target / n[len("pack/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
    try:
        assert pack_io.doctor_pack("_suite_import")["status"] == "ready"
    finally:
        shutil.rmtree(target)
        out.unlink()


@case("热插拔：删除世界包后存档继续可用，其他包不受影响")
def _():
    temp = ROOT / "packs" / "_suite_gone"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    subprocess.run([sys.executable, "engine/tools/newsave.py", "_suite_gone_save", "--pack", "_suite_gone"], capture_output=True, cwd=ROOT, check=True)
    shutil.rmtree(temp)
    sd = ROOT / "saves" / "_suite_gone_save"
    hud = Orch().get_hud_state(sd.name)
    assert hud["pack_missing"] and hud["player"]["rank"] == "凡人"
    assert ctxm.build_system_prompt(sd, role="front")
    assert pack_io.doctor_pack("cyberpunk")["status"] == "ready"


@case("体检：提醒模型配置和可执行文件混进世界包")
def _():
    temp = ROOT / "packs" / "_suite_bad"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    try:
        meta = yaml.safe_load((temp / "pack.yaml").read_text(encoding="utf-8"))
        meta["back_driver"] = {"model": "x"}
        (temp / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
        (temp / "hack.py").write_text("print(1)", encoding="utf-8")
        warnings = " ".join(pack_io.doctor_pack("_suite_bad")["warnings"])
        assert "玩家配置" in warnings and "可执行文件" in warnings, warnings
    finally:
        shutil.rmtree(temp)


@case("AI 写入世界包：可写命令和模板，不能越界或写代码")
def _():
    tctx = {}
    pid = "suite_write"
    files = {
        "pack.yaml": "id: x",
        "commands/skill.md": "$ARGUMENTS",
        "templates/world/world/canon.md": "x",
        "../evil.md": "x",
        "commands/../../evil.md": "x",
        "commands/run.py": "x",
    }
    try:
        ad.execute_tool(None, "write_pack_bundle", {"pack_id": pid, "files": files}, role="genesis", tuner_context=tctx)
        target = ROOT / "packs" / pid
        assert (target / "commands/skill.md").exists() and (target / "templates/world/world/canon.md").exists()
        assert not (target / "commands/run.py").exists()
        assert not list((ROOT / "packs").glob("evil*")) and not (target.parent.parent / "evil.md").exists()
    finally:
        shutil.rmtree(ROOT / "packs" / pid, ignore_errors=True)


# ---------------- 世界包命令 ----------------
@case("世界包命令：/yixiantian 展开规则并记入故事，可以撤回")
def _():
    sd = fresh()
    seen = {}

    def loop(_cfg, _sys, messages, *_a, **_k):
        # 也会收到幕后补规划、补问变更块的调用，所以记下每次的输入
        seen.setdefault("msgs", []).append(messages[0]["content"] if messages else "")
        return {"text": "你看见了三种结局。", "model": "scripted"}

    orch = Orch()
    with with_loop(loop):
        res = orch._run_turn_core("/yixiantian 要不要跟他走", save_name=SAVE)
    assert res["archived"] and res["command"] == "yixiantian"
    assert any("天机遮蔽" in m and "要不要跟他走" in m for m in seen["msgs"]), seen
    turns = om.parse_story_History(sd)
    assert turns[-1]["story"] == "你看见了三种结局。"
    orch.undo_last_turn(save_name=SAVE)
    assert not om.parse_story_History(sd)


@case("通用命令：回复不进故事记录，世界包命令不影响其他世界包")
def _():
    sd = fresh()
    orch = Orch()
    with with_loop(lambda *_a, **_k: {"text": "这是回顾。", "model": "scripted"}):
        res = orch._run_turn_core("/recap", save_name=SAVE)
    assert not res["archived"] and res["story"] == "这是回顾。"
    assert not om.parse_story_History(sd)
    subprocess.run([sys.executable, "engine/tools/newsave.py", "_suite_cyber", "--pack", "cyberpunk"], capture_output=True, cwd=ROOT, check=True)
    assert orch._pack_command_file(ROOT / "saves/_suite_cyber", "yixiantian") is None


# ---------------- 开局设置清单 ----------------
import pack_setup  # noqa: E402

SETUP = [
    {"id": "name", "label": "名字", "mode": "ask", "target": "sheet.name"},
    {"id": "who", "label": "身份", "mode": "ask", "type": "choice", "options": ["甲", "乙"], "required": False, "target": "canon"},
    {"id": "tone", "label": "基调", "mode": "default", "value": "黑暗", "target": "note"},
    {"id": "era", "mode": "fixed", "value": "绝密纪元", "target": "canon", "label": "纪元"},
    {"id": "ui", "mode": "fixed"},
]


@case("设置清单：内置包合规，错误的清单被体检拦下")
def _():
    assert not pack_setup.check_setup(SETUP)
    for bad in (
        [{"id": "a b", "label": "x"}],
        [{"id": "a", "mode": "maybe", "label": "x"}],
        [{"id": "a"}],
        [{"id": "a", "label": "x", "type": "choice"}],
        [{"id": "a", "mode": "fixed"}],
        [{"id": "a", "label": "x", "target": "secrets"}],
        [{"id": "a", "label": "x"}, {"id": "a", "label": "y"}],
    ):
        assert pack_setup.check_setup(bad), bad
    temp = ROOT / "packs" / "_suite_setup"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    try:
        meta = yaml.safe_load((temp / "pack.yaml").read_text(encoding="utf-8"))
        meta["setup"] = [{"id": "a"}]
        (temp / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
        assert any("label" in e for e in pack_io.doctor_pack("_suite_setup")["errors"])
    finally:
        shutil.rmtree(temp)


@case("设置清单：必答、选项和作者定死的值")
def _():
    values = pack_setup.resolve_answers(SETUP, {"name": "阿甲", "era": "玩家想改", "tone": ""}, strict=True)
    assert values == {"name": "阿甲", "tone": "黑暗", "era": "绝密纪元"}, values
    for answers, needle in (({}, "名字"), ({"name": "x", "who": "丙"}, "可选范围")):
        try:
            pack_setup.resolve_answers(SETUP, answers, strict=True)
            raise AssertionError("应当报错")
        except ValueError as exc:
            assert needle in str(exc)
    assert pack_setup.resolve_answers(SETUP, None) == {"tone": "黑暗", "era": "绝密纪元"}


@case("设置清单：前端看不到作者定死的值")
def _():
    public = pack_setup.public_setup(SETUP)
    assert [i["id"] for i in public] == ["name", "who", "tone"]
    assert "绝密纪元" not in json.dumps(public, ensure_ascii=False)
    packs = {p["id"]: p for p in pack_io.list_packs()}
    assert [i["id"] for i in packs[PACK]["setup"]] == ["name"]
    assert packs["cyberpunk"]["setup"][1]["options"]


@case("设置清单：界面步骤按声明或主题完整度决定")
def _():
    assert pack_setup.ui_mode(SETUP) == "fixed"
    assert pack_setup.ui_mode([], {"pixel_frame": {}}) == "ask"
    assert pack_setup.ui_mode([], {"pixel_frame": {"enabled": True}}) == "default"
    assert pack_setup.ui_mode([], None) == "ask"
    assert pack_setup.ui_mode([{"id": "ui", "mode": "ask"}], {"pixel_frame": {"enabled": True}}) == "ask"


@case("设置清单：回答写进存档，重复写入不重复，世界包不变")
def _():
    sd = fresh()
    before = (ROOT / "packs" / PACK / "pack.yaml").read_text(encoding="utf-8")
    values = pack_setup.resolve_answers(SETUP, {"name": "阿甲", "who": "乙"}, strict=True)
    pack_setup.apply_setup(sd, SETUP, values)
    pack_setup.apply_setup(sd, SETUP, values)
    meta, _ = common.read_frontmatter(sd / "player/sheet.md")
    assert meta["name"] == "阿甲" and meta["realm"] == "凡人"
    canon = (sd / "world/canon.md").read_text(encoding="utf-8")
    assert canon.count("- 身份：乙") == 1 and "风雷双灵根" in canon
    meta_c, _ = common.read_frontmatter(sd / "world/canon.md")
    assert meta_c.get("locked") is True
    assert yaml.safe_load((sd / "world/setup.yaml").read_text(encoding="utf-8"))["who"] == "乙"
    assert (ROOT / "packs" / PACK / "pack.yaml").read_text(encoding="utf-8") == before
    from validate import validate
    rep, _ = validate(sd)
    assert not rep.errors, rep.errors


@case("新建存档：缺必答项时不留下半成品")
def _():
    from newsave import create_save
    temp = ROOT / "packs" / "_suite_setup2"
    shutil.copytree(ROOT / "packs" / PACK, temp)
    target = ROOT / "saves" / "_suite_strict"
    try:
        meta = yaml.safe_load((temp / "pack.yaml").read_text(encoding="utf-8"))
        meta["setup"] = [{"id": "name", "label": "名字", "mode": "ask", "target": "sheet.name"}]
        (temp / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
        try:
            create_save("_suite_strict", "_suite_setup2", {})
            raise AssertionError("应当报错")
        except ValueError:
            pass
        assert not target.exists()
        create_save("_suite_strict", "_suite_setup2", {"name": "阿甲"})
        assert common.read_frontmatter(target / "player/sheet.md")[0]["name"] == "阿甲"
    finally:
        shutil.rmtree(temp)


@case("世界种子：幕后拿到玩家选择，且改不掉它")
def _():
    sd_path = ROOT / "saves" / "_suite_seed"
    orch = Orch()
    seen = {}

    def fake_sim(sd, args, _cfg, on_status=None):
        seen["req"] = args["request"]
        meta, body = common.read_frontmatter(sd / "player/sheet.md")
        common.write_frontmatter(sd / "player/sheet.md", {**meta, "name": "被幕后改掉"}, body)
        now, scene = common.read_frontmatter(sd / "world/now.md")
        common.write_frontmatter(sd / "world/now.md", {**now, "location": "loc_start"}, scene)
        common.write_frontmatter(sd / "world/locations/loc_start.md",
                                 {"id": "loc_start", "name": "起始地点", "detail": "full"}, "地点正文")
        return "简报"

    orch.run_world_sim = fake_sim
    payload = {"pack_id": PACK, "world_name": "_suite_seed", "setup_answers": {"name": "乘风"}}
    blank = pack_setup.describe_for_seed([{"id": "x", "label": "待定项", "mode": "ask", "required": False}], {})
    assert blank == "- 待定项：玩家交给你决定", blank
    res = orch._run_genesis_core("seed", payload)
    assert res["ok"], res.get("validation_errors")
    assert "主角的名字：乘风" in seen["req"]
    assert common.read_frontmatter(sd_path / "player/sheet.md")[0]["name"] == "乘风"


@case("没有 setup 的世界包照常工作")
def _():
    temp = ROOT / "packs" / "_suite_plain"
    shutil.copytree(ROOT / "packs" / "cyberpunk", temp)
    try:
        meta = yaml.safe_load((temp / "pack.yaml").read_text(encoding="utf-8"))
        meta.pop("setup")
        (temp / "pack.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True), encoding="utf-8")
        assert pack_io.doctor_pack("_suite_plain")["status"] == "ready"
        packs = {p["id"]: p for p in pack_io.list_packs()}
        assert packs["_suite_plain"]["setup"] == [] and packs["_suite_plain"]["ui_mode"] == "default"
        subprocess.run([sys.executable, "engine/tools/newsave.py", "_suite_plain_save", "--pack", "_suite_plain"], capture_output=True, cwd=ROOT, check=True)
        assert not (ROOT / "saves/_suite_plain_save/world/setup.yaml").exists()
    finally:
        shutil.rmtree(temp)


@case("终端模式：show 和 apply 命令可用")
def _():
    sd = fresh()
    shown = subprocess.run([sys.executable, "engine/tools/pack_setup.py", "show", PACK], capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=True).stdout
    assert json.loads(shown)[0]["id"] == "name"
    subprocess.run([sys.executable, "engine/tools/pack_setup.py", "apply", SAVE, "--json", json.dumps({"name": "乘风"}, ensure_ascii=False)], capture_output=True, cwd=ROOT, check=True)
    assert common.read_frontmatter(sd / "player/sheet.md")[0]["name"] == "乘风"


# ---------------- 幕后模型配置 ----------------
FRONT = {"type": "api", "api_base": "https://a.example/v1", "api_key": "front-key", "model": "fast"}


@case("幕后模型：默认和台前相同")
def _():
    assert ad.resolve_back_driver({"front_driver": FRONT, "back_driver": {"inherit_front": True, "model": "x"}})["model"] == "fast"
    assert ad.resolve_back_driver({"front_driver": FRONT})["model"] == "fast"


@case("幕后模型：同接口只换模型，切换厂商后跟着走")
def _():
    back = {"inherit_front": False, "same_endpoint": True, "model": "deep", "api_base": "https://stale.example"}
    got = ad.resolve_back_driver({"front_driver": FRONT, "back_driver": back})
    assert got["model"] == "deep" and got["api_base"] == FRONT["api_base"] and got["api_key"] == "front-key"


@case("幕后模型：另一家接口不会拿到台前密钥")
def _():
    back = {"inherit_front": False, "same_endpoint": False, "type": "api", "model": "deep", "api_base": "https://b.example/v1"}
    got = ad.resolve_back_driver({"front_driver": FRONT, "back_driver": back})
    assert got["api_base"] == "https://b.example/v1" and not got.get("api_key")
    same = dict(back, api_base=FRONT["api_base"] + "/")
    assert ad.resolve_back_driver({"front_driver": FRONT, "back_driver": same})["api_key"] == "front-key"


@case("幕后模型：Claude Code 用 --model 和子代理环境变量")
def _():
    front = {"type": "cli", "cli_tool": "claude", "cli_args": ["--model", "old", "-x"]}
    back = {"inherit_front": False, "same_endpoint": True, "model": "opus"}
    cfg = {"front_driver": front, "back_driver": back}
    assert ad.resolve_back_driver(cfg)["cli_args"] == ["-x", "--model", "opus", "--effort", "high"], "幕后默认想得更深"
    assert ad.front_driver_for_turn(cfg)["subagent_model"] == "opus"
    assert "subagent_model" not in ad.front_driver_for_turn({"front_driver": front, "back_driver": {"inherit_front": True}})
    other = {"type": "cli", "cli_tool": "codex"}
    assert "subagent_model" not in ad.front_driver_for_turn({"front_driver": other, "back_driver": back})


class ModelGate(BaseHTTPRequestHandler):
    """只接受名叫 good 的模型，模拟玩家把幕后模型名写错的情况。"""

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        ok = body.get("model") in ("good", "fast")
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = {"model": body.get("model"), "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]} if ok else {"error": {"message": "model not found"}}
        self.wfile.write(json.dumps(payload).encode())


@case("向导：幕后模型测试通过才写入；失败时台前保留、幕后不变")
def _():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ModelGate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg_path = ROOT / "config.yaml"
    original = cfg_path.read_text(encoding="utf-8")
    front = {"type": "api", "api_format": "openai", "api_base": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": "k", "model": "fast"}
    orch = Orch()
    try:
        bad = orch.verify_driver_and_save({"front_driver": front, "back_driver": {"inherit_front": False, "same_endpoint": True, "model": "nope"}})
        assert bad["check"]["ok"] and not bad["back_check"]["ok"]
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["front_driver"]["model"] == "fast" and saved["back_driver"]["inherit_front"] is True

        good = orch.verify_driver_and_save({"front_driver": front, "back_driver": {"inherit_front": False, "same_endpoint": True, "model": "good"}})
        assert good["back_check"]["ok"]
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["back_driver"]["model"] == "good" and saved["back_driver"]["inherit_front"] is False

        same = orch.verify_driver_and_save({"front_driver": front, "back_driver": {"inherit_front": True}})
        assert same["back_check"] is None
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["back_driver"]["inherit_front"] is True
    finally:
        cfg_path.write_text(original, encoding="utf-8")
        srv.shutdown()


@case("独立性：幕后配置不写进世界包，切换世界包不改配置")
def _():
    cfg_path = ROOT / "config.yaml"
    original = cfg_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(original)
    cfg["back_driver"] = {"inherit_front": False, "same_endpoint": True, "model": "deep"}
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    try:
        sd = fresh()
        seen = {}

        def loop(driver_cfg, *_a, **_k):
            seen["model"] = driver_cfg.get("model")
            return {"text": "简报", "model": "scripted"}

        with with_loop(loop):
            Orch().run_world_sim(sd, {"request": "推演"}, om.load_config())
        assert seen["model"] == "deep"
        for pid in (PACK, "cyberpunk"):
            assert "deep" not in (ROOT / "packs" / pid / "pack.yaml").read_text(encoding="utf-8")
        out = pack_io.export_pack(PACK, ROOT / "exports" / "_suite2.rpgpack")
        with zipfile.ZipFile(out) as zf:
            assert not [n for n in zf.namelist() if "config" in n]
        out.unlink()
    finally:
        cfg_path.write_text(original, encoding="utf-8")


for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
