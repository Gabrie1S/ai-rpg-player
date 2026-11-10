"""幕后世界推演与造物命令行入口：转发至运行时层 `engine/runtime/world_sim_cli.py`，保持 tools 层零运行时反向耦合。

用法：
  python engine/tools/world_sim.py --task "推演" --range "第1天~第7天" --request "结算到期事件与势力动向" [--save 存档名]
"""
import subprocess
import sys
from pathlib import Path

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "runtime" / "world_sim_cli.py"

if __name__ == "__main__":
    sys.exit(subprocess.run([sys.executable, str(CLI_SCRIPT), *sys.argv[1:]]).returncode)
