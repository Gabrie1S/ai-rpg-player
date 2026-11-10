#!/bin/sh
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 Python，需要先安装才能运行游戏。"
  echo "下载地址：https://www.python.org/downloads/"
  echo "装好以后，重新双击这个文件就可以了。"
  open "https://www.python.org/downloads/" 2>/dev/null
  printf "按回车键关闭……"
  read -r _
  exit 1
fi

python3 start.py "$@"
status=$?
if [ "$status" -ne 0 ]; then
  printf "按回车键关闭……"
  read -r _
fi
exit "$status"
