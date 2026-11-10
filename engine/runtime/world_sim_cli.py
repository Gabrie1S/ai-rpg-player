"""幕后世界推演（world-sim）运行时命令行执行器。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / "engine" / "tools"
sys.path[:0] = [str(ROOT), str(TOOLS_DIR)]

from ai_log import trace_scope  # noqa: E402
from common import load_config, save_dir  # noqa: E402
from engine.runtime.orchestrator import RPGOrchestrator, WorldSimWriteFailed  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="在独立幕后上下文中运行 world-sim 并输出无剧透简报")
    parser.add_argument("--save", default=None, help="存档名称（默认读取当前激活存档）")
    parser.add_argument("--task", default="推演", help="任务类型：推演 | 造物 | 改世界 | 整理阶段清单 | 世界快照 | 开局种子生成")
    parser.add_argument("--range", dest="day_range", default="当前", help="时间范围，如 第1天~第7天")
    parser.add_argument("--request", default="", help="具体需要 world-sim 完成的事项（留空则从 stdin 读取）")
    args = parser.parse_args()

    req_text = (args.request or "").strip()
    if not req_text and not sys.stdin.isatty():
        req_text = sys.stdin.read().strip()
    if not req_text:
        parser.error("请通过 --request 或标准输入提供具体任务说明")

    sd = save_dir(args.save)
    cfg = load_config()
    orch = RPGOrchestrator()

    with trace_scope("wsim_cli", save=sd.name, task=args.task):
        try:
            brief = orch.run_world_sim(
                sd,
                {
                    "task_type": args.task,
                    "day_range": args.day_range,
                    "request": req_text,
                },
                cfg,
            )
        except RuntimeError as exc:
            if isinstance(exc, WorldSimWriteFailed):
                print(f"【幕后推演未完成】{exc}。存档里这次想改的内容没有落地，不要按简报里说的“已完成”继续。")
                return 1
            if "外部 IDE / 终端原生 Harness 协同模式" in str(exc):
                print(
                    "【外部 IDE 协同模式提示】当前未配置独立外部 API/SDK 幕后驱动。"
                    "请直接阅读并遵循 `engine/agents/world-sim.md` 静默完成本次幕后修改与 `validate.py` 校验，"
                    "切勿将 `world/secrets/` 内容或内部判定过程写入给玩家看的正文。"
                )
                return 0
            raise
    print((brief or "").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
