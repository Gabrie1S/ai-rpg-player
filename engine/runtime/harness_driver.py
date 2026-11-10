"""Agent Harness 适配驱动器（支持 Google Antigravity Python SDK、OpenAI Agents SDK、Claude Agent SDK 及外部 IDE/终端原生 Harness 协同模式）。

支持四种主流 Harness 形态：
1. `external_ide`：外部 IDE / 终端原生 Harness 协同模式（玩家直接在 Antigravity IDE / Cursor / Windsurf / Claude Code / Codex 中打开仓库对话主持，Web 界面作为实时像素画故事台与状态面板联动）；
2. `openai_agents_sdk`：通过 OpenAI 官方 `openai-agents` SDK (`agents`) 驱动兼容任意 OpenAI `/chat/completions` 模型的 Agent 循环；
3. `antigravity_sdk`：通过 `google-antigravity` Python SDK (`Agent`, `LocalAgentConfig`, `CapabilitiesConfig`) 原生流式驱动，自动剥离 `response.thoughts` 思考链写入诊断日志；
4. `claude_agent_sdk`：通过 `claude-agent-sdk` Python SDK 驱动具备非交互沙箱权限与存档环境变量隔离的 Agent 循环。
"""
import asyncio
import importlib.util
import inspect
import os
import threading
import time
from pathlib import Path
import sys

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from ai_log import child_env_with_trace, log_error, log_event  # noqa: E402
from common import ROOT  # noqa: E402
from .api_driver import _parse_tool_arguments  # noqa: E402
from .driver_config import _credential_endpoint, _has_openai_agents_sdk, _openai_chat_url, is_local_api, resolve_api_key  # noqa: E402
from .stream_filter import (  # noqa: E402
    StreamThinkStripper,
    make_spoiler_safe_status,
    strip_think_blocks,
)
from .tool_sandbox import (  # noqa: E402
    BACK_TOOLS_OPENAI,
    FRONT_TOOLS_OPENAI,
    GENESIS_TOOLS_OPENAI,
    TUNER_TOOLS_OPENAI,
    EngineToolFault,
    ToolRunGuard,
    execute_tool,
)

HARNESS_REGISTRY = [
    {
        "id": "external_ide",
        "name": "外部 IDE / 终端原生 Harness 协同模式",
        "package": "",
        "install_hint": "无需额外安装：直接在 Antigravity IDE / Cursor / Claude Code / Codex 中打开本项目目录对话，Web 端实时呈现像素画对话框与状态栏",
    },
    {
        "id": "openai_agents_sdk",
        "name": "OpenAI Agents SDK (通用兼容)",
        "package": "agents",
        "install_hint": "pip install openai-agents",
    },
    {
        "id": "antigravity_sdk",
        "name": "Google Antigravity Python SDK（网页执行暂不可用）",
        "package": "google.antigravity",
        "install_hint": "pip install google-antigravity",
    },
    {
        "id": "claude_agent_sdk",
        "name": "Anthropic Claude Agent SDK",
        "package": "claude_agent_sdk",
        "install_hint": "pip install claude-agent-sdk",
    },
]

REQUIRED_ENTRY_MANIFESTS = ["AGENTS.md", "CLAUDE.md", "GEMINI.md"]
REQUIRED_SKILLS = [
    "narrative-craft",
    "gm-workflow",
    "pack-designer",
    "mod-builder",
    "worldbuilder",
    "genesis-onboarding",
    "pack-porter",
    "ui-designer",
    "live-tuner",
    "causal-foresight",
]


def _has_module(mod_name: str) -> bool:
    if not mod_name:
        return True
    try:
        return importlib.util.find_spec(mod_name) is not None
    except Exception:
        return False


def discover_harnesses() -> list:
    """探测当前 Python 环境与工作区可用的 Agent Harness 模式。"""
    manifests_ok = all((ROOT / m).is_file() for m in REQUIRED_ENTRY_MANIFESTS)
    skills_ok = all((ROOT / "skills" / s / "SKILL.md").is_file() for s in REQUIRED_SKILLS)
    out = []
    for item in HARNESS_REGISTRY:
        if item["id"] == "external_ide":
            avail = manifests_ok and skills_ok
        elif item["id"] == "antigravity_sdk":
            avail = False
        elif item["id"] == "openai_agents_sdk":
            avail = _has_openai_agents_sdk()
        else:
            avail = _has_module(item["package"])
        out.append({
            **item,
            "available": avail,
        })
    return out


def verify_harness_driver(driver_cfg: dict) -> dict:
    """验证当前选择的 Agent Harness 驱动是否就绪。"""
    hmode = (driver_cfg.get("harness_mode") or "external_ide").strip().lower()
    t0 = time.perf_counter()

    missing_entries = [m for m in REQUIRED_ENTRY_MANIFESTS if not (ROOT / m).is_file()]
    missing_skills = [s for s in REQUIRED_SKILLS if not (ROOT / "skills" / s / "SKILL.md").is_file()]
    if missing_entries or missing_skills:
        raise ValueError(f"工作区 Harness 规范文件缺失：entries={missing_entries}, skills={missing_skills}")

    if hmode == "antigravity_sdk":
        raise ValueError("Antigravity SDK 尚未接通引擎工具，暂不能在网页中驱动游戏。可使用外部 IDE 协同模式。")
    elif hmode == "claude_agent_sdk":
        if not _has_module("claude_agent_sdk"):
            raise ValueError(
                "尚未安装 Claude Agent SDK。请先运行 `pip install claude-agent-sdk`，或切换为 Claude Code CLI / Direct API。"
            )
        detail = "Anthropic Claude Agent SDK 已通过模型探针"
    elif hmode == "openai_agents_sdk":
        if not _has_openai_agents_sdk():
            raise ValueError(
                "尚未安装 OpenAI Agents SDK。请先运行 `pip install openai-agents`，或直接选择推荐的 Direct API (OpenAI 兼容) 模式。"
            )
        detail = "OpenAI Agents SDK 已通过模型探针"
    elif hmode == "external_ide":
        detail = "外部 IDE 协同所需的仓库文件齐全。请在 IDE 发起任务，网页负责显示结果。"
    else:
        raise ValueError(f"未知的 Harness 模式：{hmode}")

    if hmode != "external_ide":
        probe = run_harness_turn(
            driver_cfg, "", "Reply with exactly RPG_VERIFY_OK. Do not use tools.",
            role="verify", tools=[], max_steps=1,
        )
        if "RPG_VERIFY_OK" not in (probe.get("text") or "").strip().splitlines():
            raise ValueError("Harness 未返回预期的探针回复，请检查登录和模型配置")

    ms = round((time.perf_counter() - t0) * 1000, 1)
    log_event("driver.verify", mode="harness", harness_mode=hmode, ms=ms, ok=True)
    return {
        "ok": True,
        "mode": "harness",
        "harness_mode": hmode,
        "web_execution": hmode != "external_ide",
        "latency_ms": ms,
        "detail": f"{detail}（耗时 {ms}ms）",
    }


async def _run_antigravity_sdk_async(system_prompt: str, prompt_text: str, role: str, stream: bool, on_status, on_delta):
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig  # type: ignore

    cfg = LocalAgentConfig(
        system_instructions=system_prompt,
        capabilities=CapabilitiesConfig(),
    )
    thought_chunks = []
    text_chunks = []
    emitted_writing = False
    stripper = StreamThinkStripper(
        on_think_start=lambda: on_status(make_spoiler_safe_status(role, "thinking")) if on_status else None
    )

    async with Agent(cfg) as agent:
        response = await agent.chat(prompt_text)

        async def _drain_thoughts():
            if hasattr(response, "thoughts"):
                try:
                    async for th in response.thoughts:
                        if th:
                            thought_chunks.append(str(th))
                            if on_status and not emitted_writing:
                                on_status(make_spoiler_safe_status(role, "thinking"))
                except Exception:
                    pass

        async def _drain_tools():
            if hasattr(response, "tool_calls"):
                try:
                    async for call in response.tool_calls:
                        tname = getattr(call, "name", "") or "state_sync"
                        if on_status and not emitted_writing:
                            on_status(make_spoiler_safe_status(role, "tool", tname))
                        log_event("harness.tool_call", role=role, harness="antigravity_sdk", tool=tname)
                except Exception:
                    pass

        bg_tasks = [asyncio.create_task(_drain_thoughts()), asyncio.create_task(_drain_tools())]
        async for token in response:
            s = str(token or "")
            if not s:
                continue
            text_chunks.append(s)
            vis = stripper.feed(s)
            if vis and stream and on_delta and role in ("front", "tuner"):
                if not emitted_writing and on_status:
                    emitted_writing = True
                    on_status(make_spoiler_safe_status(role, "writing"))
                on_delta(vis)

        tail = stripper.finish()
        if tail and stream and on_delta and role in ("front", "tuner"):
            if not emitted_writing and on_status:
                on_status(make_spoiler_safe_status(role, "writing"))
            on_delta(tail)

        await asyncio.gather(*bg_tasks, return_exceptions=True)

    if stripper.thinking_text:
        thought_chunks.append(stripper.thinking_text)
    full_raw = "".join(text_chunks)
    clean_out, _ = strip_think_blocks(full_raw)
    return clean_out, "\n".join(thought_chunks).strip()


def _run_coro_sync(coro, timeout=None):
    """安全同步执行异步协程：若当前线程已存在运行中的事件循环（如 FastAPI/Uvicorn），则在独立工作线程中启动 loop。"""
    async def run():
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout)
        except TimeoutError as exc:
            raise ValueError("Harness 模型探针超时，请检查登录或接口配置") from exc

    try:
        asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        in_running_loop = False
    if not in_running_loop:
        return asyncio.run(run())

    box = {"res": None, "err": None}

    def _worker():
        try:
            box["res"] = asyncio.run(run())
        except BaseException as exc:
            box["err"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if box["err"] is not None:
        raise box["err"]
    return box["res"]


async def _run_claude_agent_sdk_async(driver_cfg: dict, system_prompt: str, prompt_text: str, role: str, stream: bool, on_status, on_delta):
    import claude_agent_sdk  # type: ignore

    sub_env = child_env_with_trace()
    if driver_cfg.get("save_name"):
        sub_env["RPG_SAVE"] = str(driver_cfg["save_name"])
    sub_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

    opt_cls = claude_agent_sdk.ClaudeAgentOptions
    try:
        accepted_params = set(inspect.signature(opt_cls).parameters.keys())
    except Exception:
        accepted_params = {"system_prompt", "cwd", "allowed_tools"}

    candidate_kwargs = {
        "system_prompt": system_prompt,
        "cwd": str(ROOT),
        "allowed_tools": [] if role == "verify" else ["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        "permission_mode": "default" if role == "verify" else "acceptEdits",
        "env": sub_env,
        "setting_sources": [],
    }
    if role == "verify":
        if "tools" not in accepted_params:
            raise ValueError("当前 Claude Agent SDK 无法禁用探针工具，请使用支持 tools 配置的版本")
        candidate_kwargs["tools"] = []
    if driver_cfg.get("model") and "model" in accepted_params:
        candidate_kwargs["model"] = str(driver_cfg["model"])
    opts = opt_cls(**{k: v for k, v in candidate_kwargs.items() if k in accepted_params})

    chunks = []
    emitted_writing = False
    async for msg in claude_agent_sdk.query(prompt=prompt_text, options=opts):
        if getattr(msg, "is_error", False):
            raise RuntimeError("Claude Agent SDK 报告任务执行失败")
        content = getattr(msg, "result", None) or getattr(msg, "content", None)
        if isinstance(content, str) and content:
            chunks.append(content)
            if stream and on_delta and role in ("front", "tuner"):
                if not emitted_writing and on_status:
                    emitted_writing = True
                    on_status(make_spoiler_safe_status(role, "writing"))
                on_delta(content)
    clean_out, th = strip_think_blocks("\n".join(chunks))
    return clean_out, th


async def _run_openai_agents_sdk_async(
    driver_cfg: dict,
    system_prompt: str,
    prompt_text: str,
    role: str,
    stream: bool,
    on_status,
    on_delta,
    sd=None,
    tools=None,
    world_sim_callback=None,
    tuner_context=None,
    max_steps: int = 12,
    tool_guard=None,
):
    tool_guard = tool_guard if tool_guard is not None else ToolRunGuard()
    # 熔断异常要原样交回引擎；SDK 可能把工具里的异常包一层或转成报错文字继续跑，所以单独记下
    tripped = []
    from agents import Agent, FunctionTool, RunConfig, Runner  # type: ignore
    try:
        from agents import OpenAIChatCompletionsModel  # type: ignore
    except ImportError:
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel  # type: ignore
    from openai import AsyncOpenAI  # type: ignore
    from httpx import AsyncClient

    api_key = resolve_api_key(driver_cfg)
    raw_base = _credential_endpoint(driver_cfg)
    is_local = is_local_api(raw_base)
    if not api_key and not is_local:
        raise RuntimeError(
            f"未配置 API Key（请在驱动设置中填入 api_key，或设置环境变量 {driver_cfg.get('api_key_env', 'DEEPSEEK_API_KEY')}）"
        )

    chat_url = _openai_chat_url(raw_base)
    base_url = chat_url[: -len("/chat/completions")]
    model_name = (driver_cfg.get("model") or "deepseek-chat").strip()

    role_tools_map = {
        "front": FRONT_TOOLS_OPENAI,
        "back": BACK_TOOLS_OPENAI,
        "tuner": TUNER_TOOLS_OPENAI,
        "genesis": GENESIS_TOOLS_OPENAI + [TUNER_TOOLS_OPENAI[1]],
    }
    raw_tools = role_tools_map.get(role, FRONT_TOOLS_OPENAI) if tools is None else tools
    tool_logs = []
    sdk_tools = []
    try:
        ft_params = set(inspect.signature(FunctionTool).parameters.keys())
    except Exception:
        ft_params = {"name", "description", "params_json_schema", "on_invoke_tool"}

    for spec in raw_tools or []:
        if not isinstance(spec, dict) or not isinstance(spec.get("function"), dict):
            continue
        fn_spec = spec["function"]
        fn_name = fn_spec.get("name")
        if not fn_name:
            continue

        async def _invoke(_ctx, raw_json: str, _name=fn_name):
            parsed_args, arg_err = _parse_tool_arguments(raw_json)
            if arg_err:
                log_event("harness.tool_bad_args", lvl="WARN", role=role, harness="openai_agents_sdk", tool=_name, err=arg_err)
                return f"ERROR: 工具 `{_name}` 参数 JSON 格式不合法（{arg_err}）。请提供合法的 JSON 对象后重试。"
            if on_status:
                on_status(make_spoiler_safe_status(role, "tool", _name))
            log_event("harness.tool_call", role=role, harness="openai_agents_sdk", tool=_name, args=parsed_args)
            if tripped:
                raise tripped[0]
            try:
                res_str = await asyncio.to_thread(
                    execute_tool,
                    sd,
                    _name,
                    parsed_args,
                    role=role,
                    world_sim_callback=world_sim_callback,
                    tuner_context=tuner_context,
                    guard=tool_guard,
                )
            except EngineToolFault as fault:
                tripped.append(fault)
                raise
            tool_logs.append({"tool": _name, "ok": not str(res_str).startswith("ERROR")})
            return str(res_str)

        ft_kwargs = {
            "name": fn_name,
            "description": fn_spec.get("description", ""),
            "params_json_schema": fn_spec.get("parameters") or {"type": "object", "properties": {}},
            "on_invoke_tool": _invoke,
        }
        if "strict_json_schema" in ft_params:
            ft_kwargs["strict_json_schema"] = False
        sdk_tools.append(FunctionTool(**ft_kwargs))

    client = AsyncOpenAI(api_key=api_key or "local-no-key", base_url=base_url,
                         http_client=AsyncClient(follow_redirects=False))
    try:
        chat_model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        agent = Agent(
            name=f"rpg_{role}",
            instructions=system_prompt or "",
            model=chat_model,
            tools=sdk_tools,
        )
        run_kwargs = {"input": prompt_text}
        try:
            run_sig = set(inspect.signature(Runner.run).parameters.keys())
        except Exception:
            run_sig = {"input", "max_turns", "run_config"}
        if "max_turns" in run_sig:
            run_kwargs["max_turns"] = max(1, int(max_steps or 12))
        if "run_config" in run_sig:
            run_kwargs["run_config"] = RunConfig(tracing_disabled=True)
        try:
            res = await Runner.run(agent, **run_kwargs)
        except Exception as exc:
            if tripped:
                raise tripped[0] from exc
            raise
        if tripped:
            raise tripped[0]
    finally:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            maybe_coro = close_fn()
            if inspect.isawaitable(maybe_coro):
                await maybe_coro

    raw_out = str(getattr(res, "final_output", "") or "")
    clean_out, th = strip_think_blocks(raw_out)
    if clean_out and stream and on_delta and role in ("front", "tuner"):
        if on_status:
            on_status(make_spoiler_safe_status(role, "writing"))
        on_delta(clean_out)
    return clean_out, th, tool_logs


def run_harness_turn(
    driver_cfg,
    system_prompt,
    prompt_text,
    role="front",
    stream=False,
    on_status=None,
    on_delta=None,
    sd=None,
    tools=None,
    world_sim_callback=None,
    tuner_context=None,
    max_steps=12,
    tool_guard=None,
):
    """通过选定的 Agent Harness 执行单轮任务。tool_guard 只对接引擎工具的 Harness 生效（见 ToolRunGuard）。"""
    hmode = (driver_cfg.get("harness_mode") or "external_ide").strip().lower()
    t0 = time.perf_counter()
    log_event("harness.req", role=role, harness_mode=hmode, stream=bool(stream), prompt_chars=len(prompt_text or ""))

    if on_status:
        on_status(make_spoiler_safe_status(role, "thinking"))

    if hmode == "external_ide":
        raise RuntimeError("当前是外部 IDE 协同模式。请在 IDE 推进剧情或创世，网页会同步结果。")

    tool_logs = []
    probe_timeout = 30 if role == "verify" else None
    try:
        if hmode == "antigravity_sdk":
            raise RuntimeError("Antigravity SDK 尚未接通引擎工具，无法执行网页任务")
        elif hmode == "claude_agent_sdk":
            if not _has_module("claude_agent_sdk"):
                raise RuntimeError("未检测到 `claude-agent-sdk` 包，请运行 `pip install claude-agent-sdk`")
            clean_out, think_txt = _run_coro_sync(
                _run_claude_agent_sdk_async(driver_cfg, system_prompt, prompt_text, role, stream, on_status, on_delta),
                timeout=probe_timeout,
            )
        elif hmode == "openai_agents_sdk":
            if not _has_openai_agents_sdk():
                raise RuntimeError("未检测到 `openai-agents` 包，请运行 `pip install openai-agents`")
            clean_out, think_txt, tool_logs = _run_coro_sync(
                _run_openai_agents_sdk_async(
                    driver_cfg,
                    system_prompt,
                    prompt_text,
                    role,
                    stream,
                    on_status,
                    on_delta,
                    sd=sd,
                    tools=tools,
                    world_sim_callback=world_sim_callback,
                    tuner_context=tuner_context,
                    max_steps=max_steps,
                    tool_guard=tool_guard,
                ),
                timeout=probe_timeout,
            )
        else:
            raise RuntimeError(f"不支持的 Harness 模式：{hmode}")
    except Exception as exc:
        log_error("harness.err", exc=exc, role=role, harness_mode=hmode)
        raise

    ms = round((time.perf_counter() - t0) * 1000, 1)
    if think_txt:
        log_event("harness.think", role=role, harness_mode=hmode, chars=len(think_txt), reasoning=think_txt)
    log_event("harness.resp", role=role, harness_mode=hmode, ms=ms, chars=len(clean_out), tools_used=len(tool_logs))
    return {
        "text": clean_out,
        "tool_logs": tool_logs,
        "model": f"Harness ({hmode})",
    }
