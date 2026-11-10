"""Comprehensive automated verification suite for AI-RPG-Player upgrades."""
import ast
import base64
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine" / "tools"))

# 原型项目题材用语（编码存放，避免词表本身触发扫描）
BANNED_TERMS = base64.b64decode("5L+u5LuZLOS7meS+oCzngbXmoLks54K85rCULOetkeWfuizph5HkuLks5YWD5am0LOWMluelnizlpKfkuZgs5rih5YqrLOS4gOe6v+WkqSzkuZ3ltrcs5rOV5a6dLOeBteefsyzpgZPlj4ss5a6X6ZeoLOS4ueiNryxsaW5nZ2VuLHlpeGlhbnRpYW4seGlhbnhpYQ==").decode().split(",")


def step(title):
    print(f"\n=== [TEST] {title} ===")


def test_1_compile_and_decoupling():
    step("1. Python Syntax Compilation & Architectural Decoupling Boundary")
    skip_py = {".git", "__pycache__", ".venv", "venv", "env", "saves"}
    py_files = [
        p for p in ROOT.rglob("*.py")
        if not any(part in skip_py for part in p.relative_to(ROOT).parts)
    ]
    assert len(py_files) >= 10, f"Expected >=10 py files, got {len(py_files)}"
    for p in py_files:
        py_compile.compile(str(p), doraise=True)

        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(p))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("engine/tools/"):
            for imp in imports:
                assert not imp.startswith(("engine.runtime", "runtime", "web")), (
                    f"Boundary violation in {rel}: imported {imp}"
                )
        elif rel.startswith("engine/runtime/"):
            for imp in imports:
                assert not imp.startswith("web"), (
                    f"Boundary violation in {rel}: imported {imp}"
                )
    print(f"OK: Compiled {len(py_files)} Python files; decoupling boundaries verified.")


def test_2_zero_parent_terms():
    step("2. Zero Parent-Project Term Leakage Scan")
    # 引擎必须与题材无关；世界包本身就是题材内容，说明文档和测试可以提到它们，所以不扫描
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "env", "saves", "exports", "logs", ".slices", "packs", "tests"}
    skip_root_docs = {"README.md", "ARCHITECTURE_REPORT.md", "config.local.yaml"}
    checked = 0
    hits = []
    for p in ROOT.rglob("*"):
        if any(part in skip_dirs for part in p.relative_to(ROOT).parts) or p.name in skip_root_docs:
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".py", ".md", ".yaml", ".yml", ".json", ".html", ".css", ".js", ".txt"}:
            continue
        checked += 1
        txt = p.read_text(encoding="utf-8", errors="replace")
        for term in BANNED_TERMS:
            if re.search(re.escape(term), txt, flags=re.IGNORECASE):
                for lineno, line in enumerate(txt.splitlines(), 1):
                    if term.lower() in line.lower():
                        hits.append((p.relative_to(ROOT).as_posix(), lineno, term, line.strip()[:100]))
    if hits:
        print("FOUND BANNED TERMS:")
        for rel, ln, term, snippet in hits:
            print(f"  - {rel}:{ln} [{term}] -> {snippet}")
        raise AssertionError(f"Found {len(hits)} parent-project term occurrences!")
    print(f"OK: Scanned {checked} project files — 0 parent-project terms found.")


def test_3_tools_and_sandbox():
    step("3. CLI Tools, Context Dual-Channel Notes, Statusline & Sandboxed Rollback")
    from common import PACKS, save_dir
    from context import assemble_turn_context, build_system_prompt, extract_inline_director_notes, format_context_markdown
    from engine.runtime.api_driver import execute_tool
    from pack_io import doctor_pack
    from statusline import format_widget_item, game_state

    # 3a. Inline director note extraction
    clean, notes = extract_inline_director_notes("我推开酒馆木门（场外：酒保还不认识我，语气要冷淡）(OOC: keep tension high)")
    assert clean == "我推开酒馆木门", f"Unexpected clean input: {clean!r}"
    assert len(notes) == 2, f"Expected 2 notes, got {notes}"

    # Normal parentheses without OOC prefix should stay in-character
    clean2, notes2 = extract_inline_director_notes("我拔出武器（左手持匕首）冲上前")
    assert "左手持匕首" in clean2 and notes2 == [], f"In-character parens should remain: {clean2!r}, {notes2}"

    # 3b. Cyberpunk Pack Doctor
    rep = doctor_pack("cyberpunk")
    assert rep["valid"] is True and rep["status"] == "ready", f"Cyberpunk doctor failed: {rep}"

    # 3c. Context assembly & 4-role system prompts
    sd = save_dir(required=False)
    assert sd is not None, "Expected active test save"
    turn_ctx = assemble_turn_context(sd, "检查装备（导演：不要跳过细节）")
    ctx_md = format_context_markdown(turn_ctx)
    assert "不要跳过细节" in ctx_md, "Inline director note missing from formatted context!"
    assert turn_ctx["clean_action"] == "检查装备"

    for role in ("front", "back", "tuner", "genesis"):
        sp = build_system_prompt(sd if role != "genesis" else None, role=role)
        assert isinstance(sp, str) and len(sp) > 100, f"Empty system prompt for role={role}"

    # 3d. Statusline game_state & adaptive widgets
    gstate = game_state(sd.name)
    assert gstate is not None and len(gstate.get("world_status_items", [])) >= 3
    meter_w = format_widget_item({"key": "overload", "label": "过载", "widget": "meter", "max": 100}, 42)
    assert meter_w["widget"] == "meter" and meter_w["percent"] == 42.0
    tags_w = format_widget_item({"key": "implants", "label": "义体", "widget": "tags"}, ["义眼", "神经加速器"])
    assert tags_w["widget"] == "tags" and len(tags_w["tags"]) == 2

    # 3e. Sandboxed update_world_ui rollback on invalid widget
    tmp_pack = PACKS / "_test_ui_rollback"
    if tmp_pack.exists():
        shutil.rmtree(tmp_pack)
    shutil.copytree(PACKS / "cyberpunk", tmp_pack)
    try:
        tctx = {"pack_id": "_test_ui_rollback"}
        bad_res = execute_tool(
            sd,
            "update_world_ui",
            {
                "ui_schema_patch": {
                    "player_status": [{"key": "hp", "label": "HP", "widget": "INVALID_WIDGET_XYZ"}]
                },
                "summary": "bad patch",
            },
            tuner_context=tctx,
        )
        assert "回滚" in bad_res, f"Expected rollback message, got: {bad_res}"
        # Verify pack is still healthy after rollback
        after_rep = doctor_pack("_test_ui_rollback")
        assert after_rep["valid"] is True, f"Rollback did not restore valid pack: {after_rep}"

        # Valid UI update should succeed
        good_res = execute_tool(
            sd,
            "update_world_ui",
            {
                "theme_patch": {"css_vars": {"--accent": "#00f0ff"}},
                "summary": "adjust accent",
            },
            tuner_context=tctx,
        )
        assert "✓" in good_res and tctx.get("ui_updated") is True, f"Expected valid UI update: {good_res}"
    finally:
        if tmp_pack.exists():
            shutil.rmtree(tmp_pack)
    print("OK: Dual-channel notes, 4-role context, statusline widgets, and UI doctor rollback verified.")


def test_4_fault_tolerance_and_empty_packs():
    step("4. Pack Fault Tolerance: Nested Flatten, Broken YAML, Empty Packs & Missing Active Pack")
    from common import PACKS, save_dir
    from context import assemble_turn_context, build_system_prompt, format_context_markdown
    from pack_io import doctor_pack, flatten_pack, list_packs
    from web.server import handle_get_state

    # 4a. Nested pack detection & flatten
    nested_dir = PACKS / "_test_nested"
    if nested_dir.exists():
        shutil.rmtree(nested_dir)
    try:
        inner = nested_dir / "inner_world"
        shutil.copytree(PACKS / "cyberpunk", inner)
        packs_list = {p["id"]: p for p in list_packs()}
        assert "_test_nested" in packs_list
        assert packs_list["_test_nested"]["status"] == "nested", f"Got: {packs_list['_test_nested']}"
        assert packs_list["_test_nested"]["nested_subpath"] == "inner_world"

        flat_res = flatten_pack("_test_nested")
        assert flat_res["flattened"] is True, f"Flatten failed: {flat_res}"
        rep_after = doctor_pack("_test_nested")
        assert rep_after["valid"] is True and rep_after["status"] == "ready", f"After flatten: {rep_after}"
    finally:
        if nested_dir.exists():
            shutil.rmtree(nested_dir)

    # 4b. Malformed YAML & unparsed folder resilience
    broken_dir = PACKS / "_test_broken"
    if broken_dir.exists():
        shutil.rmtree(broken_dir)
    try:
        broken_dir.mkdir(parents=True)
        (broken_dir / "pack.yaml").write_text("::: invalid: [yaml: unclosed", encoding="utf-8")
        (broken_dir / "lore_notes.md").write_text("# 散装世界观笔记\n蒸汽与深海的世界。", encoding="utf-8")
        packs_list = {p["id"]: p for p in list_packs()}
        assert "_test_broken" in packs_list
        binfo = packs_list["_test_broken"]
        assert binfo["valid"] is False
        assert len(binfo["errors"]) > 0
        # Ensure handle_get_state does not crash while broken pack is in packs/
        st = handle_get_state()
        assert "saves" in st and "onboarding" in st
    finally:
        if broken_dir.exists():
            shutil.rmtree(broken_dir)

    # 4c. Empty packs/ & active save pack deleted externally!
    tmp_backup_root = Path(tempfile.mkdtemp(prefix="rpg_pack_bak_"))
    moved = []
    try:
        for item in list(PACKS.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                dest = tmp_backup_root / item.name
                shutil.move(str(item), str(dest))
                moved.append((dest, item))

        assert list_packs() == [], "Expected empty list_packs() when packs/ is empty"

        # Active save (`霓虹测试`) still references `cyberpunk` which is now missing!
        st_missing = handle_get_state()
        assert st_missing["pack_missing"] is True, f"Expected pack_missing=True, got {st_missing.get('pack_missing')}"
        assert st_missing["missing_pack_id"] == "cyberpunk"
        assert st_missing["onboarding"]["needs_onboarding"] is True
        assert st_missing["onboarding"]["ready_packs_count"] == 0

        # Context assembly & system prompt on orphaned save still works via save-local fallback!
        sd = save_dir(required=False)
        ctx_orphan = format_context_markdown(assemble_turn_context(sd, "环顾四周"))
        sp_orphan = build_system_prompt(sd, role="front")
        assert len(ctx_orphan) > 100 and "存档内置设定兜底" in sp_orphan, "Expected save-local fallback when external pack is missing"
        st_no_save = handle_get_state("__nonexistent__")
        assert st_no_save["onboarding"]["needs_onboarding"] is True
    finally:
        for src_bak, orig_dest in moved:
            if src_bak.exists():
                shutil.move(str(src_bak), str(orig_dest))
        shutil.rmtree(tmp_backup_root, ignore_errors=True)

    # Confirm cyberpunk is cleanly restored
    assert doctor_pack("cyberpunk")["valid"] is True, "Cyberpunk must be valid after restore!"
    print("OK: Nested flatten, broken YAML tolerance, empty-packs onboarding detection, and orphan-save fallback all passed!")


def test_5_live_http_endpoints():
    step("5. Live Web Server HTTP Endpoints Check (StdlibRPGHandler)")
    from web.server import StdlibRPGHandler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), StdlibRPGHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for path in (
            "/",
            "/static/app.js",
            "/static/themes.css",
            "/api/state",
            "/api/packs",
            "/api/packs/detail?pack_id=cyberpunk",
            "/api/packs/doctor?pack_id=cyberpunk",
        ):
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                assert resp.status == 200, f"GET {path} returned {resp.status}"
                body = resp.read().decode("utf-8")
                assert len(body) > 10, f"Empty body for {path}"
                if path == "/":
                    assert 'id="tuner-dock"' in body, "Missing fixed #tuner-dock in index.html"
                    assert 'id="genesis-modal"' in body, "Missing #genesis-modal in index.html"
                    assert 'class="system-topbar"' in body, "Missing fixed .system-topbar in index.html"
                elif path == "/api/state":
                    data = json.loads(body)
                    assert data["pack_id"] == "cyberpunk"
                    assert data["pack_missing"] is False
                    assert "tuner_history" in data and "onboarding" in data

        # POST /api/tuner/clear
        req = urllib.request.Request(
            base + "/api/tuner/clear",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            res_json = json.loads(resp.read().decode("utf-8"))
            assert res_json.get("ok") is True
    finally:
        srv.shutdown()
        srv.server_close()
    print("OK: All HTTP GET/POST endpoints returned 200 OK with expected DOM & JSON structures.")


def test_6_dom_id_crosscheck():
    step("6. Frontend HTML <-> JS DOM ID Cross-Check")
    static_dir = ROOT / "web" / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    defined_ids = set(re.findall(r'id=["\']([^"\']+)["\']', html)) | set(re.findall(r'id=["\']([^"\']+)["\']', js))
    used_ids = set(re.findall(r'(?:getElementById|\$)\(["\']([^"\']+)["\']\)', js))
    missing = sorted(uid for uid in used_ids if uid not in defined_ids and "${" not in uid)
    assert not missing, f"DOM IDs referenced in app.js but missing in index.html/app.js: {missing}"
    print(f"OK: All {len(used_ids)} JS DOM ID references match {len(defined_ids)} defined IDs.")


if __name__ == "__main__":
    test_1_compile_and_decoupling()
    test_2_zero_parent_terms()
    test_3_tools_and_sandbox()
    test_4_fault_tolerance_and_empty_packs()
    test_5_live_http_endpoints()
    test_6_dom_id_crosscheck()
    print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY!")
