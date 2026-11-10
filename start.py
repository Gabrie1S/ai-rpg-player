"""一键启动：自动准备运行环境、启动游戏服务器、打开浏览器。

普通玩家双击「启动游戏.bat」（Windows）或「启动游戏.command」（Mac）即可。
也可以运行 python start.py，可选参数：
  --no-browser   不自动打开浏览器
  --use-venv     总是使用项目内的独立环境 .venv

这个文件只用 Python 标准库，没装任何依赖时也能运行。
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MINIMAL_MARKER = VENV / ".minimal_ok"
HOST = "127.0.0.1"
REQUIRED_MODULES = ("yaml", "fastapi", "uvicorn", "httpx")
MINIMAL_MODULES = ("yaml",)
# 官方源连不上时再试国内镜像，安装的是同样的包
PIP_INDEXES = (None, "https://pypi.tuna.tsinghua.edu.cn/simple")
PYTHON_URL = "https://www.python.org/downloads/"
GIT_URL = "https://git-scm.com/downloads"


def say(text=""):
    print(text, flush=True)


class StartupError(Exception):
    """给玩家看的启动问题，内容里写清楚该怎么办。"""


# ---------- 运行环境 ----------
def venv_python():
    folder = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return VENV / folder


def has_modules(python, modules=REQUIRED_MODULES):
    code = "import importlib.util as u,sys;sys.exit(0 if all(u.find_spec(m) for m in %r) else 1)" % (tuple(modules),)
    try:
        return subprocess.run([str(python), "-c", code], capture_output=True).returncode == 0
    except OSError:
        return False


def install_requirements(python):
    for index in PIP_INDEXES:
        cmd = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt")]
        if index:
            say("官方源连接不上，换国内镜像再试一次……")
            cmd += ["-i", index]
        if subprocess.run(cmd).returncode == 0:
            if MINIMAL_MARKER.exists():
                try:
                    MINIMAL_MARKER.unlink()
                except OSError:
                    pass
            return True
    # 兜底：web/server.py 与 api_driver.py 均内置零外部依赖的标准库 HTTP/SSE 引擎，只要装上 pyyaml 即可完整运行
    for index in PIP_INDEXES:
        cmd = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "pyyaml>=6.0"]
        if index:
            cmd += ["-i", index]
        if subprocess.run(cmd).returncode == 0 and has_modules(python, MINIMAL_MODULES):
            try:
                MINIMAL_MARKER.write_text("pyyaml\n", encoding="utf-8")
            except OSError:
                pass
            say("已启用轻量标准库模式（核心依赖 PyYAML 已就绪）。")
            return True
    return False


def prepare_python(force_venv=False):
    """返回可以运行游戏的 Python。第一次运行时自动装好依赖，放在项目内，不影响系统。"""
    if venv_python().exists():
        if has_modules(venv_python()):
            return venv_python()
        if MINIMAL_MARKER.exists() and has_modules(venv_python(), MINIMAL_MODULES):
            return venv_python()
    if not force_venv and all(importlib.util.find_spec(m) for m in REQUIRED_MODULES):
        return Path(sys.executable)
    if not force_venv and os.environ.get("RPG_MINIMAL_OK") == "1" and all(importlib.util.find_spec(m) for m in MINIMAL_MODULES):
        return Path(sys.executable)

    say("第一次运行，正在准备运行环境，大约需要一两分钟，请不要关闭这个窗口……")
    if not venv_python().exists():
        result = subprocess.run([sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise StartupError(
                "无法创建运行环境。\n"
                "如果你用的是 Linux，请先安装 python3-venv，然后重新启动。\n"
                f"详细信息：{result.stderr.strip()[:300]}"
            )
    if not install_requirements(venv_python()):
        raise StartupError("下载依赖失败，请检查网络连接后重新启动。\n重新启动时会接着安装，不用做别的操作。")
    say("运行环境准备好了。")
    return venv_python()


# ---------- 端口 ----------
def configured_port():
    """从 config.yaml 读端口。这时可能还没有 yaml 库，所以直接匹配 web 段里的 port。"""
    try:
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        match = re.search(r"(?m)^web:\s*\n(?:[ \t]+.*\n)*?[ \t]+port:\s*(\d+)", text)
        return int(match.group(1)) if match else 8000
    except (OSError, ValueError):
        return 8000


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, port)) != 0


def is_game_server(port, timeout=1.5):
    """这个端口上跑的是不是本游戏（重复双击启动时直接打开已有的页面）。"""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/state", timeout=timeout) as resp:
            return "has_save" in json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False


def choose_port(preferred):
    """返回 (端口, 是否已经在运行)。端口被别的程序占用时自动往后找。"""
    for port in range(preferred, preferred + 30):
        if port_is_free(port):
            return port, False
        if is_game_server(port):
            return port, True
    raise StartupError(f"端口 {preferred} 到 {preferred + 29} 都被占用了，请关闭一些程序后重新启动。")


# ---------- 启动 ----------
def wait_until_ready(port, process, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if is_game_server(port):
            return True
        time.sleep(0.4)
    return False


def show_url(url):
    say()
    say("=" * 50)
    say("  游戏已启动！")
    say()
    say(f"  游戏地址：{url}")
    say()
    say("  浏览器没有自动打开的话，把上面的地址复制到")
    say("  浏览器的地址栏就可以了。")
    say()
    say("  玩的时候请不要关闭这个窗口。")
    say("  不玩了就关闭这个窗口，进度每回合都会自动保存。")
    say("=" * 50)
    say()


def open_browser(url, enabled):
    if not enabled:
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass  # 地址已经显示在窗口里，玩家可以手动打开


def stop_server(process):
    """连同服务器启动的 AI 工具一起结束，避免后台还有进程在改存档。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        process.terminate()


def run(args):
    if sys.version_info < (3, 9):
        raise StartupError(f"需要 Python 3.9 或更新的版本，你现在的版本是 {sys.version.split()[0]}。\n请到这里下载安装：{PYTHON_URL}")

    say("正在启动 AI-RPG-Player……")
    if not shutil.which("git"):
        say(f"提示：没有检测到 Git。游戏可以正常玩，但“撤回”和“重演”会受限制。需要的话到这里安装：{GIT_URL}")

    port, already_running = choose_port(configured_port())
    url = f"http://{HOST}:{port}"
    if already_running:
        say(f"游戏已经在运行了，直接打开页面：{url}")
        open_browser(url, not args.no_browser)
        return 0

    python = prepare_python(force_venv=args.use_venv)
    py_bin_dir = str(Path(python).resolve().parent)
    cur_path = os.environ.get("PATH", "")
    norm_py = os.path.normcase(os.path.normpath(py_bin_dir))
    rest_path = [
        p for p in (cur_path.split(os.pathsep) if cur_path else [])
        if p and os.path.normcase(os.path.normpath(p.strip().strip('"'))) != norm_py
    ]
    merged_path = os.pathsep.join([py_bin_dir] + rest_path)
    env = {
        **os.environ,
        "PATH": merged_path,
        "RPG_WEB_PORT": str(port),
        "RPG_QUIET": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    process = subprocess.Popen([str(python), str(ROOT / "web" / "server.py")], cwd=str(ROOT), env=env)
    try:
        if not wait_until_ready(port, process):
            raise StartupError("游戏服务器没有正常启动，上面可能有错误信息。\n可以把这个窗口的内容发给帮你的人，或者重新启动试试。")
        show_url(url)
        open_browser(url, not args.no_browser)
        code = process.wait()
        if code != 0:
            raise StartupError(f"游戏服务器意外退出了（错误码 {code}），上面可能有错误信息。重新启动即可继续，进度不会丢。")
        return 0
    except KeyboardInterrupt:
        say("正在退出……")
        return 0
    finally:
        stop_server(process)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="一键启动 AI-RPG-Player")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--use-venv", action="store_true", help="总是使用项目内的独立环境")
    try:
        return run(parser.parse_args())
    except StartupError as exc:
        say()
        say("启动遇到问题：")
        say(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
