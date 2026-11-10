"""主流 Agent CLI 桥接驱动器（支持 Claude Code / OpenAI Codex / Gemini CLI / Antigravity CLI / OpenCode）。

当玩家将驱动设为 `type: cli` 时，通过非交互子进程驱动本机已登录的主流 CLI 执行主持、推演、创世与调教：
- 自动将当前 `RPG_TRACE_ID` 注入子进程环境变量，使 CLI 内部调用的 Python 工具日志与当前回合全链路关联；
- 自动剥离 `<think>...</think>` 思考块并仅向前端推送无剧透阶段状态。
"""
import hashlib
import json
import shutil
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from ai_log import child_env_with_trace, log_error, log_event  # noqa: E402
from common import ROOT  # noqa: E402
from engine.runtime.stream_filter import (  # noqa: E402
    StreamThinkStripper,
    make_spoiler_safe_status,
    strip_think_blocks,
)

CLI_REGISTRY = [
    {
        "id": "claude",
        "name": "Claude Code CLI (claude)",
        "entry_file": "CLAUDE.md",
        "desc": "Anthropic 官方 Agent CLI，支持完整工具链与 .claude/ 子代理",
    },
    {
        "id": "codex",
        "name": "OpenAI Codex CLI (codex)",
        "entry_file": "AGENTS.md",
        "desc": "OpenAI 官方终端 Agent，原生读取 AGENTS.md 与 skills/",
    },
    {
        "id": "gemini",
        "name": "Google Gemini CLI (gemini)",
        "entry_file": "GEMINI.md",
        "desc": "Google 官方终端 Agent，原生读取 GEMINI.md 与 skills/",
    },
    {
        "id": "agy",
        "name": "Google Antigravity CLI (agy)",
        "entry_file": "AGENTS.md",
        "desc": "Antigravity 终端交互界面，原生支持 AGENTS.md 与技能体系",
    },
    {
        "id": "opencode",
        "name": "OpenCode CLI (opencode)",
        "entry_file": "AGENTS.md",
        "desc": "开源多模型终端 Agent，支持非交互 `opencode run`",
    },
]


def check_cli_available(cli_tool: str):
    tool_name = (cli_tool or "claude").strip()
    exe = shutil.which(tool_name)
    return bool(exe), exe or ""


def discover_installed_clis() -> list:
    """扫描系统 PATH 中已安装的主流 Agent CLI 列表。"""
    out = []
    for item in CLI_REGISTRY:
        ok, exe = check_cli_available(item["id"])
        out.append({
            **item,
            "available": ok,
            "executable": exe,
        })
    return out


MAX_SAFE_ARGV_CHARS = 8000
STDIN_INSTRUCTION = "请严格遵循标准输入(stdin)中传入的完整系统设定、当前场景切片与玩家指令执行本轮任务，直接输出结果。"


DEFAULT_CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"

READONLY_PLAIN_GUARD = (
    "【纯文本只读模式：本轮严禁调用任何读写文件或执行命令的工具，请直接输出文本结果。】\n\n"
)
READONLY_TURN_GUARD = (
    "【只读回合沙箱：本轮严禁直接写入/编辑任何文件或执行命令行脚本；"
    "确需查阅未加载文件只能只读查看，所有存档状态变更必须且只能写在回复末尾的 <state_changes> JSON 块内。】\n\n"
)


def _strip_claude_only_tools(tools: str) -> str:
    if not tools:
        return ""
    kept = [t.strip() for t in tools.split(",") if t.strip() and t.strip() != "Agent"]
    return ",".join(kept)


def _build_cli_invocation(exe: str, cli_tool: str, prompt_text: str, extra_args: list, tools: str = None):
    """构造跨平台安全的 CLI 调用命令与 stdin 载荷（彻底规避 Windows CreateProcess 32KB 命令行长度上限，并对全部 CLI 区分只读/写权限）。"""
    stem = Path(exe).stem.lower()
    tool_key = (cli_tool or stem).lower()
    if "claude" in stem or tool_key == "claude":
        eff_tools = DEFAULT_CLAUDE_TOOLS if tools is None else tools
        perm_args = []
        if any(w in eff_tools for w in ("Write", "Edit", "Bash")) and "--permission-mode" not in extra_args:
            perm_args = ["--permission-mode", "acceptEdits"]
        cmd = [
            exe,
            "-p",
            STDIN_INSTRUCTION,
            # --allowedTools 只管免确认，不会减少发给模型的工具说明；--tools 才会。tools="" 表示不给任何工具
            "--tools",
            eff_tools,
        ] + (["--allowedTools", eff_tools] if eff_tools != "" else []) + perm_args + extra_args
        return cmd, prompt_text, None

    eff_non_claude_tools = _strip_claude_only_tools(DEFAULT_CLAUDE_TOOLS if tools is None else tools)
    is_readonly = eff_non_claude_tools in ("", "Read,Grep,Glob")

    if "codex" in stem or tool_key == "codex":
        codex_flags = []
        if not any(a in extra_args for a in ("--full-auto", "--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox")):
            if is_readonly:
                codex_flags = ["--sandbox", "read-only"]
            else:
                codex_flags = ["--full-auto"]
        return [exe, "exec"] + codex_flags + extra_args + ["-"], prompt_text, None

    guarded_prompt = prompt_text or ""
    if is_readonly and guarded_prompt:
        guard = READONLY_PLAIN_GUARD if eff_non_claude_tools == "" else READONLY_TURN_GUARD
        guarded_prompt = guard + guarded_prompt

    if "gemini" in stem or tool_key == "gemini":
        gem_flags = []
        if not is_readonly and not any(a in extra_args for a in ("-y", "--yolo", "--approval-mode")):
            gem_flags = ["-y"]
        return [exe, "-p", STDIN_INSTRUCTION] + gem_flags + extra_args, guarded_prompt, None

    # 对于 opencode / agy 或自定义 CLI：短文本走参数，长文本自动落临时文件并通过短指令引用 + stdin 双保险
    if len(guarded_prompt) <= MAX_SAFE_ARGV_CHARS:
        if "opencode" in stem or tool_key == "opencode":
            return [exe, "run", guarded_prompt] + extra_args, None, None
        if "agy" in stem or tool_key == "agy":
            if extra_args:
                return [exe] + extra_args + [guarded_prompt], None, None
            return [exe, "-p", guarded_prompt], None, None
        return [exe] + extra_args + [guarded_prompt], None, None

    tmp_dir = ROOT / "logs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / f".cli_prompt_{int(time.time() * 1000)}_{threading.get_ident()}.md"
    tmp_file.write_text(guarded_prompt, encoding="utf-8")
    rel_tmp = tmp_file.relative_to(ROOT).as_posix()
    short_ref = f"请先完整阅读 `{rel_tmp}`（或标准输入 stdin）中的上下文与指令，严格照此执行并输出结果。"
    if "opencode" in stem or tool_key == "opencode":
        return [exe, "run", short_ref] + extra_args, guarded_prompt, tmp_file
    if "agy" in stem or tool_key == "agy":
        if extra_args:
            return [exe] + extra_args + [short_ref], guarded_prompt, tmp_file
        return [exe, "-p", short_ref], guarded_prompt, tmp_file
    return [exe] + extra_args + [short_ref], guarded_prompt, tmp_file


def verify_cli_driver(driver_cfg: dict) -> dict:
    """检查版本后，用实际的非交互入口验证模型能回复。"""
    cli_tool = (driver_cfg.get("cli_tool") or "claude").strip()
    t0 = time.perf_counter()
    ok, exe = check_cli_available(cli_tool)
    if not ok:
        raise ValueError(
            f"未在系统 PATH 中检测到 CLI 工具「{cli_tool}」。请确认已在终端安装并配置 PATH，或切换为 Direct API / Harness 模式。"
        )

    try:
        proc = subprocess.run(
            [exe, "--version"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"CLI ({cli_tool}) 无法完成版本检查：{exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"CLI ({cli_tool}) 版本检查失败（退出码 {proc.returncode}）")
    version_lines = ((proc.stdout or "") + " " + (proc.stderr or "")).strip().splitlines()
    ver_str = version_lines[0] if version_lines else ""
    probe = run_cli_turn(
        driver_cfg, "Reply with exactly RPG_VERIFY_OK. Do not use tools.",
        role="verify", tools="",
    )
    if "RPG_VERIFY_OK" not in (probe.get("text") or "").splitlines():
        raise ValueError(f"CLI ({cli_tool}) 未返回预期的探针回复，请检查登录和模型配置")

    ms = round((time.perf_counter() - t0) * 1000, 1)
    log_event("driver.verify", mode="cli", cli_tool=cli_tool, executable=exe, version=ver_str[:120], ms=ms, ok=True)
    return {
        "ok": True,
        "mode": "cli",
        "cli_tool": cli_tool,
        "executable": exe,
        "version": ver_str[:120],
        "latency_ms": ms,
        "detail": f"CLI 驱动检查通过：{cli_tool} ({ver_str[:60] or exe} · {ms}ms)",
    }


# 世界生成这类任务会跑很久，上限只用来防止进程卡死
CLI_MAX_SECONDS = {"front": 900, "tuner": 1200, "back": 2400, "genesis": 2400, "verify": 30}
HEARTBEAT_SECONDS = 5


def _kill_tree(proc):
    """连子进程一起结束，否则 CLI 启动的子代理还会继续改存档。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def _start_watchdog(proc, role, on_status, kind):
    """定时发状态：玩家能看到还在运行，点取消时状态回调会抛出异常，这里负责结束进程。"""
    limit = CLI_MAX_SECONDS.get(role, 900)
    stop = threading.Event()
    state = {"error": None}

    def loop():
        started = time.monotonic()
        while not stop.wait(HEARTBEAT_SECONDS):
            if proc.poll() is not None:
                return
            try:
                if on_status:
                    on_status(make_spoiler_safe_status(role, kind["value"]))
                if time.monotonic() - started > limit:
                    raise RuntimeError(f"AI 工具运行超过 {limit // 60} 分钟没有结束，已停止。可以重试。")
            except Exception as exc:
                state["error"] = exc
                _kill_tree(proc)
                return

    threading.Thread(target=loop, daemon=True).start()
    return stop, state


def _is_claude(exe, cli_tool):
    return "claude" in Path(exe).stem.lower() or (cli_tool or "").lower() == "claude"


def _claude_session_args(stream, system_file, replace_system, needs_project):
    """去掉这次调用用不到的东西：技能列表、MCP、用户和项目设置、会话记录。每一样都是每次调用都要带的 token。"""
    args = ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
    args += ["--setting-sources", "project" if needs_project else ""]
    if system_file:
        args += ["--system-prompt-file" if replace_system else "--append-system-prompt-file", str(system_file)]
    if stream:
        args += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    else:
        args += ["--output-format", "json"]
    return args


def _write_system_file(text):
    """按内容命名：内容不变时文件不变，也方便排查这一轮到底发了什么。"""
    folder = ROOT / "logs" / ".cli_system"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    path = folder / f"{digest}.md"
    if not path.exists():
        path.write_text(text, encoding="utf-8")
        cutoff = time.time() - 7 * 86400
        for old in folder.glob("*.md"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    return path


def _log_claude_usage(role, data):
    usage = data.get("usage") or {}
    log_event(
        "cli.usage",
        role=role,
        model=",".join((data.get("modelUsage") or {}).keys()),
        api_calls=data.get("num_turns"),
        input=usage.get("input_tokens"),
        cache_write=usage.get("cache_creation_input_tokens"),
        cache_read=usage.get("cache_read_input_tokens"),
        output=usage.get("output_tokens"),
        duration_ms=data.get("duration_ms"),
        duration_api_ms=data.get("duration_api_ms"),
        cost_usd=data.get("total_cost_usd"),
    )


def _claude_result_text(data):
    if data.get("is_error"):
        raise RuntimeError(f"AI 工具返回错误：{str(data.get('result') or data.get('subtype') or '未知错误')[:500]}")
    return str(data.get("result") or "")


def run_cli_turn(driver_cfg, prompt_text, role="front", stream=False, on_status=None, on_delta=None,
                 system_prompt=None, tools=None, replace_system=False, needs_project=False, on_step_reset=None):
    """system_prompt 是这一局基本不变的内容。Claude Code 放进系统提示才能跨轮命中缓存；其他工具拼在提示词最前面。"""
    cli_tool = (driver_cfg.get("cli_tool") or "claude").strip()
    extra_args = list(driver_cfg.get("cli_args") or [])
    ok, exe = check_cli_available(cli_tool)
    if not ok:
        raise RuntimeError(
            f"未在系统 PATH 中找到 CLI 工具「{cli_tool}」。请确认已安装该 CLI，或在驱动设置中切换为 Direct API / Harness 模式。"
        )

    is_claude = _is_claude(exe, cli_tool)
    if is_claude:
        system_file = _write_system_file(system_prompt) if system_prompt else None
        extra_args = _claude_session_args(bool(stream and on_delta), system_file, replace_system, needs_project) + extra_args
    elif system_prompt:
        prompt_text = f"{system_prompt}\n\n---\n\n{prompt_text}"

    cmd, stdin_payload, tmp_prompt_file = _build_cli_invocation(exe, cli_tool, prompt_text, extra_args, tools)
    structured = "--output-format" in cmd
    sub_env = child_env_with_trace()
    if driver_cfg.get("subagent_model"):
        sub_env["CLAUDE_CODE_SUBAGENT_MODEL"] = str(driver_cfg["subagent_model"])
    # 引擎给这一次调用的工具脚本传的约定（如回写记录文件、推演进度由引擎记录），与具体 CLI 无关
    for env_key, env_val in (driver_cfg.get("engine_env") or {}).items():
        sub_env[str(env_key)] = str(env_val)
    # 隔离：工具脚本只认这一局，不跟随 saves/current.txt；Claude Code 的项目记忆按目录共享，会跨世界串内容
    if driver_cfg.get("save_name"):
        sub_env["RPG_SAVE"] = str(driver_cfg["save_name"])
    sub_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    t0 = time.perf_counter()
    log_event("cli.req", role=role, cli_tool=cli_tool, stream=bool(stream), prompt_chars=len(prompt_text or ""), via_stdin=bool(stdin_payload))

    if on_status:
        on_status(make_spoiler_safe_status(role, "thinking"))

    try:
        if not stream or on_delta is None or role not in ("front", "tuner"):
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(ROOT),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=sub_env,
                )
                stop, watch = _start_watchdog(proc, role, on_status, {"value": "thinking"})
                out_text, err_text = proc.communicate(input=stdin_payload)
                stop.set()
                if watch["error"]:
                    raise watch["error"]
            except Exception as exc:
                if proc is not None and proc.poll() is None:
                    _kill_tree(proc)
                log_error("cli.err", exc=exc, role=role, cli_tool=cli_tool)
                raise

            ms = round((time.perf_counter() - t0) * 1000, 1)
            if proc.returncode != 0:
                err_msg = (err_text or out_text or "").strip()[:500]
                log_event("cli.nonzero", lvl="ERR", role=role, cli_tool=cli_tool, rc=proc.returncode, stderr=err_text, stdout=out_text, ms=ms)
                raise RuntimeError(f"CLI ({cli_tool}) 执行返回非零退出码 ({proc.returncode})：{err_msg}")

            if structured:
                try:
                    data = json.loads(out_text or "{}")
                except json.JSONDecodeError:
                    data = {"result": out_text}
                _log_claude_usage(role, data)
                out_text = _claude_result_text(data)
            clean_out, think_txt = strip_think_blocks(out_text or "")
            if think_txt:
                log_event("cli.think", role=role, cli_tool=cli_tool, chars=len(think_txt), reasoning=think_txt)
            log_event("cli.resp", role=role, cli_tool=cli_tool, ms=ms, chars=len(clean_out))
            return {
                "text": clean_out,
                "tool_logs": [],
                "model": f"CLI ({cli_tool})",
            }

        # 流式子进程读取（异步写 stdin + 独立守护线程排空 stderr 防管道死锁 + 剥离 <think>）
        raw_chunks = []
        stderr_chunks = []
        emitted_writing = False
        stripper = StreamThinkStripper(
            on_think_start=lambda: on_status(make_spoiler_safe_status(role, "thinking")) if on_status else None
        )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=sub_env,
            )

            def _feed_stdin():
                if proc.stdin is not None:
                    try:
                        if stdin_payload:
                            proc.stdin.write(stdin_payload)
                            proc.stdin.flush()
                    except Exception:
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass

            def _drain_stderr():
                if proc.stderr is not None:
                    try:
                        for err_line in proc.stderr:
                            stderr_chunks.append(err_line)
                    except Exception:
                        pass

            if stdin_payload is not None:
                threading.Thread(target=_feed_stdin, daemon=True).start()
            err_thread = threading.Thread(target=_drain_stderr, daemon=True)
            err_thread.start()

            beat = {"value": "thinking"}
            stop, watch = _start_watchdog(proc, role, on_status, beat)
            assert proc.stdout is not None
            final_result = None
            emitted_this_message = False
            if structured:
                # 一行一个事件：既能真正逐字显示，也能拿到实际用量
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        final_result = event
                        continue
                    if event.get("type") != "stream_event":
                        continue
                    inner = event.get("event") or {}
                    if inner.get("type") == "message_start":
                        # 模型读完文件后开始新的一段：之前的文字不是正文
                        if emitted_this_message and on_step_reset:
                            on_step_reset()
                        emitted_this_message = False
                    elif inner.get("type") == "content_block_delta" and (inner.get("delta") or {}).get("type") == "text_delta":
                        vis = (inner["delta"].get("text") or "")
                        if vis:
                            emitted_this_message = True
                            if not emitted_writing and on_status:
                                emitted_writing = True
                                beat["value"] = "writing"
                                on_status(make_spoiler_safe_status(role, "writing"))
                            on_delta(vis)
            while not structured:
                ch = proc.stdout.read(24)
                if not ch:
                    break
                raw_chunks.append(ch)
                vis = stripper.feed(ch)
                if vis:
                    if not emitted_writing and on_status:
                        emitted_writing = True
                        beat["value"] = "writing"
                        on_status(make_spoiler_safe_status(role, "writing"))
                    on_delta(vis)

            rc = proc.wait(timeout=60)
            stop.set()
            if watch["error"]:
                raise watch["error"]
            err_thread.join(timeout=2.0)
            stderr_txt = "".join(stderr_chunks)
        except Exception as exc:
            # 前端「停止生成」或异常中断时，立即终止子进程，避免孤儿 CLI 进程在后台继续改写存档
            _p = locals().get("proc")
            if _p is not None and _p.poll() is None:
                _kill_tree(_p)
            log_error("cli.err", exc=exc, role=role, cli_tool=cli_tool)
            raise

        tail_vis = stripper.finish() if not structured else ""
        if tail_vis:
            if not emitted_writing and on_status:
                on_status(make_spoiler_safe_status(role, "writing"))
            on_delta(tail_vis)

        ms = round((time.perf_counter() - t0) * 1000, 1)
        full_raw = "".join(raw_chunks)
        if structured and final_result is not None:
            _log_claude_usage(role, final_result)
            if rc == 0:
                full_raw = _claude_result_text(final_result)
        if rc != 0:
            err_msg = (stderr_txt or full_raw).strip()[:500]
            log_event("cli.nonzero", lvl="ERR", role=role, cli_tool=cli_tool, rc=rc, stderr=stderr_txt, stdout=full_raw, ms=ms)
            raise RuntimeError(f"CLI ({cli_tool}) 执行返回非零退出码 ({rc})：{err_msg}")

        if stripper.thinking_text:
            log_event("cli.think", role=role, cli_tool=cli_tool, chars=len(stripper.thinking_text), reasoning=stripper.thinking_text)
        clean_final, _ = strip_think_blocks(full_raw)
        log_event("cli.resp", role=role, cli_tool=cli_tool, ms=ms, chars=len(clean_final))
        return {
            "text": clean_final,
            "tool_logs": [],
            "model": f"CLI ({cli_tool})",
        }
    finally:
        if tmp_prompt_file is not None and tmp_prompt_file.exists():
            try:
                tmp_prompt_file.unlink()
            except Exception:
                pass
