"""一键启动器的回归测试。只应由 tests/run_all.py 在临时副本中运行。"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import start  # noqa: E402

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


class Handler(http.server.BaseHTTPRequestHandler):
    body = b"{}"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)


def serve(body):
    handler = type("H", (Handler,), {"body": body})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@case("端口：读取配置，缺失时用默认值")
def _():
    assert start.configured_port() == 8000
    original = start.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        start.ROOT = Path(tmp)
        assert start.configured_port() == 8000
        (Path(tmp) / "config.yaml").write_text("other:\n  port: 1\nweb:\n  host: 127.0.0.1\n  port: 9123\n", encoding="utf-8")
        assert start.configured_port() == 9123
    start.ROOT = original


@case("端口：被别的程序占用时往后找，不把它当成游戏")
def _():
    srv = serve(b"not json")
    try:
        port, running = start.choose_port(srv.server_port)
        assert not running and port != srv.server_port
        assert start.port_is_free(port)
    finally:
        srv.shutdown()


@case("端口：游戏已在运行时复用，不重复启动")
def _():
    srv = serve(json.dumps({"has_save": True}).encode())
    try:
        assert start.choose_port(srv.server_port) == (srv.server_port, True)
    finally:
        srv.shutdown()


@case("实际启动：服务器起来、显示地址、页面可访问，重复启动不会多开")
def _():
    port = free_port()
    config = ROOT / "config.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original.replace("port: 8000", f"port: {port}"), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "start.py", "--no-browser"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not any("游戏地址" in l for l in lines):
            assert proc.poll() is None, "".join(lines)[-500:]
            time.sleep(0.3)
        assert any(f"http://127.0.0.1:{port}" in l for l in lines), "".join(lines)[-500:]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
        again = subprocess.run([sys.executable, "start.py", "--no-browser"], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert again.returncode == 0 and "已经在运行" in again.stdout, again.stdout
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True) if os.name == "nt" else proc.kill()
        config.write_text(original, encoding="utf-8")


@case("启动失败：提示写清楚，不显示程序报错堆栈")
def _():
    original = start.choose_port

    def boom(_):
        raise start.StartupError("端口都被占用了")

    start.choose_port = boom
    argv = sys.argv
    sys.argv = ["start.py", "--no-browser"]
    try:
        assert start.main() == 1
    finally:
        start.choose_port = original
        sys.argv = argv


@case("新手电脑：Git 没配置身份时，存档快照和撤回照常工作")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "gitconfig"
        empty.write_text("", encoding="utf-8")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "HOME": tmp, "USERPROFILE": tmp, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            env.pop(key, None)
        name = "_suite_noident"
        subprocess.run([sys.executable, "engine/tools/newsave.py", name, "--pack", "cyberpunk"], cwd=ROOT, env=env, check=True, capture_output=True)
        sd = ROOT / "saves" / name
        script = (
            "import sys;sys.path.insert(0,'engine/tools');from pathlib import Path;import apply as a,timeline as t,mods as m;"
            f"sd=Path('saves/{name}');"
            "a.archive_story_turn(sd,'行动','正文。');assert a.git_turn_checkpoint(sd,'行动',prefix='[turn]');"
            "m.write_files(sd,{'rules.md':'规则'});"
            "r=t.undo_last_turn(sd);assert r['via_git'] and r['kept'],r;assert (sd/'mods/rules.md').exists()"
        )
        done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        assert done.returncode == 0, done.stderr[-500:]
        log = subprocess.run(["git", "-C", str(sd), "log", "--format=%s"], capture_output=True, text=True, encoding="utf-8", env=env).stdout
        assert "新建世界" in log, log
        shutil.rmtree(sd, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))


@case("双击脚本：Windows 用 CRLF 换行，Mac 用 LF，都调用 start.py")
def _():
    bat = (ROOT / "启动游戏.bat").read_bytes()
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")
    assert b"start.py" in bat and b"chcp 65001" in bat
    assert bat.isascii(), "批处理切到 UTF-8 后按字节重读脚本，脚本里有中文会读错"
    assert (ROOT / "engine/docs/no_python.txt").exists()
    mac = (ROOT / "启动游戏.command").read_bytes()
    assert b"\r" not in mac and mac.startswith(b"#!/bin/sh") and b"start.py" in mac


for ok, name, detail in results:
    print(("OK  " if ok else "FAIL") + " " + name + (f"\n       {detail}" if detail else ""))
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
