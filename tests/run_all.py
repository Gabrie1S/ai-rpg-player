"""回归测试入口：python tests/run_all.py（只跑其中几组：python tests/run_all.py suite_turns.py suite_write_guard.py）

测试会新建世界包和存档、改写 config.yaml，所以全部在仓库的临时副本里运行，不碰真实存档。
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SUITES = [
    "suite_core.py",
    "suite_streaming.py",
    "suite_drivers_ui.py",
    "suite_timeline.py",
    "suite_pack.py",
    "suite_mods.py",
    "suite_jobs.py",
    "suite_turns.py",
    "suite_isolation.py",
    "suite_launcher.py",
    "suite_universal.py",
    "suite_write_guard.py",
]
SKIP = shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "env", "logs", "exports", "saves", ".slices", "settings.local.json", "config.local.yaml")


def force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def use_test_config(path):
    """测试绝不能用玩家配好的驱动：否则会真的调用 CLI 或付费接口，也不该把密钥带进副本。"""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg["active_pack"] = "cyberpunk"
    cfg["front_driver"] = {
        "type": "api",
        "provider": "openai_compat",
        "api_base": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",
        "model": "deepseek-chat",
        "cli_tool": "claude",
        "cli_args": [],
    }
    cfg["back_driver"] = {"inherit_front": True, "same_endpoint": True, "model": ""}
    cfg["web"] = {**(cfg.get("web") or {}), "host": "127.0.0.1", "port": 8000}
    cfg.pop("driver_verified", None)
    cfg.pop("driver_verified_at", None)
    cfg.pop("driver_verified_for", None)
    path.with_name("config.local.yaml").unlink(missing_ok=True)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main():
    suites = [s for s in sys.argv[1:] if s in SUITES] or SUITES
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONFAULTHANDLER": "1",
        "RPG_MINIMAL_OK": "1",
    }
    tmp = Path(tempfile.mkdtemp(prefix="rpg-tests-"))
    copy = tmp / "repo"
    failed = []
    try:
        shutil.copytree(REPO, copy, ignore=SKIP)
        use_test_config(copy / "config.yaml")
        (copy / "saves").mkdir()
        (copy / "exports").mkdir()
        subprocess.run(
            [sys.executable, "engine/tools/newsave.py", "霓虹测试", "--pack", "cyberpunk"],
            cwd=copy, env=env, check=True, capture_output=True,
        )
        for suite in suites:
            use_test_config(copy / "config.yaml")
            print(f"\n########## {suite}", flush=True)
            code = subprocess.run([sys.executable, f"tests/{suite}"], cwd=copy, env=env).returncode
            if code != 0:
                print(f"{suite} 退出码 {code}（{code & 0xFFFFFFFF:#x}）", flush=True)
                failed.append(suite)
    finally:
        shutil.rmtree(tmp, onerror=force_remove)
    print("\n" + (f"失败：{failed}" if failed else f"全部通过（{len(suites)} 组）"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
