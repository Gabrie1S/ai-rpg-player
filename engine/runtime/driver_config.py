"""多后端驱动配置解析、供应商预设、思考强度映射与就绪探针。"""
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from common import load_config  # noqa: E402

API_PROVIDER_PRESETS = [
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "api_format": "openai",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "back_model": "deepseek-reasoner",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    {
        "id": "openai",
        "name": "OpenAI 官方",
        "api_format": "openai",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "back_model": "o3",
        "api_key_env": "OPENAI_API_KEY",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude 原生",
        "api_format": "anthropic",
        "api_base": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-5-5",
        "back_model": "claude-opus-5-5",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    {
        "id": "gemini",
        "name": "Google Gemini (OpenAI 兼容端)",
        "api_format": "openai",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "back_model": "gemini-2.5-pro",
        "api_key_env": "GEMINI_API_KEY",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter 聚合网关",
        "api_format": "openai",
        "api_base": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-4.6",
        "back_model": "anthropic/claude-opus-4.6",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow 硅基流动",
        "api_format": "openai",
        "api_base": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "back_model": "deepseek-ai/DeepSeek-R1",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
    {
        "id": "ollama",
        "name": "Ollama 本地免密推理",
        "api_format": "openai",
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:14b",
        "back_model": "qwen2.5:14b",
        "api_key_env": "",
    },
]

# 思考强度对所有驱动都生效：Claude 命令行转成 --effort，API 转成各家的思考强度参数（见 api_effort_fields）
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_FRONT_EFFORT = "low"
DEFAULT_BACK_EFFORT = "high"


def _detect_api_format(driver_cfg: dict) -> str:
    """优先遵循显式协议（`openai` / `anthropic`）；`auto` 或未填时，官方 `api.anthropic.com` 与 `/messages` 自动走原生 Anthropic 协议保留 Prompt Cache，其余一律走通用 OpenAI `/chat/completions`。"""
    fmt = str(driver_cfg.get("api_format") or driver_cfg.get("provider") or "").strip().lower()
    base = str(driver_cfg.get("api_base") or "").strip().lower().rstrip("/")
    if fmt == "openai":
        return "openai"
    if fmt == "anthropic":
        return "anthropic"
    if "api.anthropic.com" in base or base.endswith("/messages"):
        return "anthropic"
    return "openai"


def _openai_chat_url(api_base: str) -> str:
    """规范化 OpenAI `/chat/completions` 端点地址（兼容只填域名根路径、带 `/v1` 或已填完整路径）。"""
    b = (api_base or "https://api.deepseek.com/v1").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    parsed = urllib.parse.urlparse(b)
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        return f"{b}/v1/chat/completions"
    return f"{b}/chat/completions"


def _anthropic_messages_url(api_base: str) -> str:
    b = (api_base or "https://api.anthropic.com/v1").rstrip("/")
    if b.endswith("/messages"):
        return b
    if b.endswith("/v1"):
        return f"{b}/messages"
    return f"{b}/v1/messages"


def resolve_api_key(driver_cfg):
    key = (driver_cfg.get("api_key") or "").strip()
    if key:
        return key
    env_name = (driver_cfg.get("api_key_env") or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    url = urllib.parse.urlsplit(_credential_endpoint(driver_cfg))
    if url.scheme != "https" or url.username is not None or url.port not in (None, 443):
        return ""
    host_env = {
        "api.deepseek.com": "DEEPSEEK_API_KEY",
        "api.openai.com": "OPENAI_API_KEY",
        "api.anthropic.com": "ANTHROPIC_API_KEY",
        "generativelanguage.googleapis.com": "GEMINI_API_KEY",
        "openrouter.ai": "OPENROUTER_API_KEY",
        "api.siliconflow.cn": "SILICONFLOW_API_KEY",
    }
    return os.environ.get(host_env.get(url.hostname, ""), "").strip()


def _credential_endpoint(cfg):
    sdk = cfg.get("type") == "harness" and cfg.get("harness_mode") == "openai_agents_sdk"
    default = "https://api.anthropic.com/v1" if not sdk and _detect_api_format(cfg) == "anthropic" else "https://api.deepseek.com/v1"
    return str(cfg.get("api_base") or default).strip().rstrip("/")


def merge_driver_config(existing, incoming):
    """Keep saved credentials only when the endpoint they belong to is unchanged."""
    incoming = {k: v for k, v in incoming.items() if k != "has_saved_key"}
    candidate = {**existing, **incoming}
    if _credential_endpoint(candidate) != _credential_endpoint(existing):
        candidate["api_key"] = incoming.get("api_key") or ""
        candidate["api_key_env"] = incoming.get("api_key_env") or ""
    elif "api_key" in incoming and not str(incoming["api_key"] or "").strip():
        if "api_key" in existing:
            candidate["api_key"] = existing["api_key"]
        else:
            candidate.pop("api_key")
    return candidate


def is_local_api(api_base):
    return urllib.parse.urlsplit(api_base).hostname in ("localhost", "127.0.0.1", "::1")


def driver_log_meta(driver):
    """只记录排障需要的驱动标识，避免保存整份配置、凭证或带查询参数的 URL。"""
    kind = str(driver.get("type") or "api").lower()
    data = {"type": kind, "model": driver.get("model"), "effort": driver.get("effort")}
    if kind == "cli":
        data["cli"] = Path(driver.get("cli_tool") or "claude").name
    elif kind == "harness":
        data["harness"] = driver.get("harness_mode") or "external_ide"
    if is_api_like_driver(driver):
        try:
            url = urllib.parse.urlsplit(_credential_endpoint(driver))
            data.update(host=url.hostname, port=url.port, protocol=(
                _detect_api_format(driver) if kind == "api" else "openai"
            ))
        except ValueError:
            data["endpoint_invalid"] = True
    return data


def _probe_local_http(url: str, timeout: float = 0.45) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def _is_claude_cli(driver_cfg):
    return str(driver_cfg.get("cli_tool") or "claude").strip().lower() == "claude"


def _with_cli_model(cli_args, model):
    args = [str(a) for a in (cli_args or [])]
    if "--model" in args:
        idx = args.index("--model")
        del args[idx : idx + 2]
    return args + ["--model", model]


def _with_effort(driver_cfg, effort):
    driver = dict(driver_cfg)
    effort = (effort or "").strip().lower() or "auto"
    driver["effort"] = effort
    if driver.get("type") == "cli" and _is_claude_cli(driver):
        args = [str(a) for a in (driver.get("cli_args") or [])]
        if "--effort" in args:
            idx = args.index("--effort")
            del args[idx : idx + 2]
        driver["cli_args"] = args + (["--effort", effort] if effort != "auto" else [])
    return driver


def front_effort(front_cfg):
    """台前思考强度。老配置写在 cli_effort 里，玩家自己在命令行参数里写了 --effort 的也照样认。"""
    fc = front_cfg or {}
    args = [str(a) for a in (fc.get("cli_args") or [])]
    from_args = args[args.index("--effort") + 1] if "--effort" in args[:-1] else ""
    return str(fc.get("effort") or fc.get("cli_effort") or from_args or DEFAULT_FRONT_EFFORT).strip().lower()


def resolve_back_driver(cfg):
    """按玩家配置解析幕后 world-sim 实际使用的驱动。配置只来自 config.yaml，与世界包无关。"""
    front = dict((cfg or {}).get("front_driver") or {})
    back = dict((cfg or {}).get("back_driver") or {})
    model = str(back.get("model") or "").strip()
    if back.get("inherit_front", True):
        resolved = dict(front)
    elif back.get("same_endpoint"):
        resolved = dict(front)
        if model:
            resolved["model"] = model
            if front.get("type") == "cli" and _is_claude_cli(front):
                resolved["cli_args"] = _with_cli_model(front.get("cli_args"), model)
    else:
        # 单独的接口：不继承台前密钥，除非两边就是同一个接口，避免把密钥发给另一家服务
        resolved = {k: v for k, v in back.items() if k not in ("inherit_front", "same_endpoint", "effort") and v not in ("", None, [])}
        resolved.setdefault("type", "api")
        same_host = _credential_endpoint(resolved) == _credential_endpoint(front)
        if same_host and not resolved.get("api_key") and front.get("api_key"):
            resolved["api_key"] = front["api_key"]
    # 幕后要排剧情、算因果，玩家又不用等它出字，所以即使和台前同一个模型，也默认想得更深
    resolved.pop("cli_effort", None)
    return _with_effort(resolved, str(back.get("effort") or DEFAULT_BACK_EFFORT))


def front_driver_for_turn(cfg):
    """台前本轮的驱动。Claude Code 的幕后子代理由它自己启动，所以把幕后模型通过环境变量带给它。"""
    front = dict((cfg or {}).get("front_driver") or {})
    back = (cfg or {}).get("back_driver") or {}
    if front.get("type") == "cli" and _is_claude_cli(front):
        if not back.get("inherit_front", True) and back.get("model"):
            front["subagent_model"] = str(back["model"]).strip()
    effort = front_effort(front)
    front.pop("cli_effort", None)
    return _with_effort(front, effort)


def api_effort_fields(api_fmt, api_base, model, effort):
    """把思考强度换成 API 请求里的参数。不认的接口会报 400，调用方去掉这些参数重发一次。"""
    effort = (effort or "").strip().lower()
    if effort in ("", "auto"):
        return {}
    if api_fmt == "anthropic":
        return {"output_config": {"effort": effort}}
    if "deepseek" in f"{api_base} {model}".lower():
        # DeepSeek 只认 low / high / xhigh / max
        return {"reasoning_effort": {"medium": "high"}.get(effort, effort)}
    # OpenAI 及多数兼容接口只认 low / medium / high
    return {"reasoning_effort": {"xhigh": "high", "max": "high"}.get(effort, effort)}


def resolve_api_timeout(driver_cfg: dict, role: str = "front", streaming: bool = False) -> float:
    """根据角色与思考强度自适应放宽单步 HTTP/SSE 超时上限，避免高强度推理模型在长思考阶段断流。"""
    custom = driver_cfg.get("timeout")
    if isinstance(custom, (int, float)) and custom > 0:
        return float(custom)
    effort = str(driver_cfg.get("effort") or "").strip().lower()
    if role in ("back", "genesis") or effort in ("high", "xhigh", "max"):
        return 420.0
    return 180.0 if streaming else 150.0


def _has_openai_agents_sdk() -> bool:
    """精确检测官方 `openai-agents` SDK，避免把其它同名 `agents` Python 包误判为已安装。"""
    import importlib.metadata
    import importlib.util

    mod = sys.modules.get("agents")
    if mod is not None:
        return all(hasattr(mod, attr) for attr in ("Agent", "Runner", "OpenAIChatCompletionsModel"))
    try:
        if importlib.util.find_spec("agents") is None:
            return False
    except Exception:
        return False
    try:
        importlib.metadata.version("openai-agents")
        return True
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        return False


def is_api_like_driver(driver_cfg: dict) -> bool:
    """判断驱动是否使用引擎内置沙箱工具协议（Direct API 或 OpenAI Agents SDK）。"""
    dtype = str((driver_cfg or {}).get("type") or "api").strip().lower()
    if dtype not in ("cli", "harness"):
        return True
    if dtype == "harness":
        return str((driver_cfg or {}).get("harness_mode") or "").strip().lower() == "openai_agents_sdk"
    return False


def driver_fingerprint(cfg):
    front = cfg.get("front_driver") or {}
    back = cfg.get("back_driver") or {}
    # 环境变量中的 Key 更换后也要重验，且不把原始 Key 存进验证记录。
    effective = [{k: v for k, v in driver.items() if k != "effort"} for driver in (front, back)]
    for driver in (front, resolve_back_driver(cfg)):
        if is_api_like_driver(driver):
            effective.append(resolve_api_key(driver))
    return hashlib.sha256(json.dumps(effective, sort_keys=True).encode("utf-8")).hexdigest()


def has_driver_ready(cfg=None):
    """网页就绪需要当前配置的实测记录。外部 IDE 是手动协作模式。"""
    cfg = load_config() if cfg is None else cfg
    fd = cfg.get("front_driver") or {}
    if fd.get("type") == "harness" and fd.get("harness_mode", "external_ide") in ("external_ide", "antigravity_sdk"):
        return False
    try:
        return bool(cfg.get("driver_verified") and cfg.get("driver_verified_for") == driver_fingerprint(cfg))
    except (ValueError, TypeError):
        return False
