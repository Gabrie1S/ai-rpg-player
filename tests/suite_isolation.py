"""世界之间不能串内容的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "engine" / "tools")]

import common  # noqa: E402
import context as ctxm  # noqa: E402
import mods  # noqa: E402
import engine.runtime.orchestrator as om  # noqa: E402
from engine.runtime import api_driver as ad, cli_driver as cd  # noqa: E402

Orch = next(c for c in vars(om).values() if inspect.isclass(c) and hasattr(c, "_run_turn_core"))
results = []
# 每个世界独有的词，出现在另一个世界的提示词里就是串了
XIANXIA_WORD, CYBER_WORD = "风雷双灵根", "新巴别塔"


def case(name):
    def deco(fn):
        try:
            fn()
            results.append((True, name, ""))
        except Exception as exc:
            results.append((False, name, f"{type(exc).__name__}: {str(exc)[:500]}"))
        return fn
    return deco


def fresh(name, pack):
    sd = ROOT / "saves" / name
    if sd.exists():
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
    subprocess.run([sys.executable, "engine/tools/newsave.py", name, "--pack", pack], capture_output=True, cwd=ROOT, check=True)
    return sd


def full_prompt(sd):
    ctx = ctxm.assemble_turn_context(sd, "四处看看", om.load_config())
    return "\n".join([
        ctxm.build_system_prompt(sd, role="front"),
        ctxm.build_system_prompt(sd, role="back"),
        ctxm.format_context_markdown(ctx),
    ])


@case("两个世界的提示词互不包含对方的内容")
def _():
    a, b = fresh("_iso_a", "xianxia"), fresh("_iso_b", "cyberpunk")
    pa, pb = full_prompt(a), full_prompt(b)
    assert XIANXIA_WORD in pa and CYBER_WORD not in pa
    assert CYBER_WORD in pb and XIANXIA_WORD not in pb


@case("切换当前存档和当前世界包，不影响指定存档的内容")
def _():
    a = ROOT / "saves" / "_iso_a"
    before = full_prompt(a)
    orch = Orch()
    orch.select_pack("cyberpunk", auto_switch_save=True)
    common_path = ROOT / "saves" / "current.txt"
    common_path.write_text("_iso_b", encoding="utf-8")
    assert full_prompt(a) == before
    theme = orch.get_hud_state("_iso_a")["theme"]
    assert theme.get("id") == "ink-wash", theme.get("id")


@case("世界包被删除时，存档不会换用别的世界包")
def _():
    temp = ROOT / "packs" / "_iso_gone"
    shutil.copytree(ROOT / "packs" / "xianxia", temp)
    sd = fresh("_iso_gone_save", "_iso_gone")
    shutil.rmtree(temp)
    assert common.active_pack_id(sd) == ""
    assert common.pack_dir(sd=sd) is None
    assert not common.load_theme(sd)
    prompt = full_prompt(sd)
    assert CYBER_WORD not in prompt, "串进了别的世界"
    hud = Orch().get_hud_state(sd.name)
    assert hud["pack_missing"] and hud["theme"] == {}
    assert [t for t in hud["ui_schema"]["player_status"]], "界面回退到内置默认"


@case("没有绑定世界包的旧存档仍可跟随全局配置")
def _():
    sd = fresh("_iso_old", "cyberpunk")
    world = yaml.safe_load((sd / "world/world.yaml").read_text(encoding="utf-8"))
    world.pop("pack")
    (sd / "world/world.yaml").write_text(yaml.safe_dump(world, allow_unicode=True), encoding="utf-8")
    assert common.active_pack_id(sd) in common.available_pack_ids()


class EnvCli:
    def __enter__(self):
        self.saved = (cd._build_cli_invocation, cd.check_cli_available)
        code = "import os,json;print(json.dumps({k:os.environ.get(k) for k in ('RPG_SAVE','CLAUDE_CODE_DISABLE_AUTO_MEMORY')}))"
        cd._build_cli_invocation = lambda *_a: ([sys.executable, "-c", code], None, None)
        cd.check_cli_available = lambda *_a: (True, "fake")

    def __exit__(self, *_):
        cd._build_cli_invocation, cd.check_cli_available = self.saved


@case("外部 AI 工具：绑定这一局，关闭跨目录共享的记忆")
def _():
    with EnvCli():
        res = cd.run_cli_turn({"cli_tool": "fake", "save_name": "_iso_a"}, "提示", role="back")
    env = json.loads(res["text"])
    assert env["RPG_SAVE"] == "_iso_a", env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1", env


@case("工具脚本：RPG_SAVE 优先于全局当前存档")
def _():
    (ROOT / "saves/current.txt").write_text("_iso_b", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "engine/tools/clock.py", "now"],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
        env={**os.environ, "RPG_SAVE": "_iso_a", "PYTHONUTF8": "1"},
    )
    a_cal = common.day_to_date(0, common.load_world(ROOT / "saves/_iso_a").get("calendar"))
    assert a_cal in out.stdout, out.stdout


@case("世界驱动：回合和推演都带上这一局的名字")
def _():
    a = ROOT / "saves" / "_iso_a"
    seen = {}
    orch = Orch()
    orig_load, orig_cli = om.load_config, om.run_cli_turn
    cfg = orig_load()
    om.load_config = lambda: {**cfg, "front_driver": {"type": "cli", "cli_tool": "claude"}, "back_driver": {"inherit_front": True}}

    def fake(driver_cfg, *_a, **_k):
        seen.setdefault("names", []).append(driver_cfg.get("save_name"))
        return {"text": "正文"}

    om.run_cli_turn = fake
    try:
        orch.run_world_sim(a, {"request": "推演"}, om.load_config())
        orch._run_turn_core("行动", save_name="_iso_a")
        orch._run_tuner_core("改一下", save_name="_iso_a")
    finally:
        om.load_config, om.run_cli_turn = orig_load, orig_cli
    # 推演、（剧情规划不够时的）回合前推演、台前、调教，每次都带这一局的名字
    assert len(seen["names"]) >= 3 and set(seen["names"]) == {"_iso_a"}, seen


@case("调教文风：只改这一局，不影响其他世界和全局配置")
def _():
    a, b = fresh("_iso_a", "xianxia"), fresh("_iso_b", "cyberpunk")
    config_before = (ROOT / "config.yaml").read_text(encoding="utf-8")
    ctx = {"pack_id": "xianxia"}
    out = ad.execute_tool(a, "update_narrative_style", {"new_authors_note_template": "这一局专属的提示词", "style_rule_to_append": "这一局专属的文风", "note": "调文风"}, role="tuner", tuner_context=ctx)
    assert "✓" in out, out
    assert (ROOT / "config.yaml").read_text(encoding="utf-8") == config_before
    assert "这一局专属的提示词" in full_prompt(a) and "这一局专属的文风" in full_prompt(a)
    assert "这一局专属" not in full_prompt(b)
    assert "这一局专属" not in (ROOT / "packs/xianxia/style_pack.md").read_text(encoding="utf-8")


@case("调教记录和定制按存档分开")
def _():
    a, b = fresh("_iso_a", "xianxia"), fresh("_iso_b", "cyberpunk")
    orch = Orch()
    orch._save_tuner_history(a, [{"role": "user", "content": "甲世界的聊天"}])
    mods.write_files(a, {"rules.md": "甲世界的规则"})
    assert orch.get_tuner_history("_iso_b") == []
    assert mods.list_mods(b) == []
    assert "甲世界的规则" not in full_prompt(b)


@case("长任务进行中不能切换存档和世界包")
def _():
    sys.path.insert(0, str(ROOT / "web"))
    import server
    server.orchestrator._begin_job("seed", "_iso_a")
    try:
        for call in (
            lambda: server.handle_select_save({"name": "_iso_b"}),
            lambda: server.handle_select_pack({"pack_id": "cyberpunk"}),
            lambda: server.handle_create_save({"name": "_iso_new", "pack_id": "cyberpunk"}),
        ):
            try:
                call()
                raise AssertionError("应当拒绝")
            except ValueError as exc:
                assert "还没做完" in str(exc)
    finally:
        server.orchestrator._end_job()


for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
