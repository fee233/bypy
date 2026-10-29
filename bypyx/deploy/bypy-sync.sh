#!/bin/bash
# 兼容入口：bypy 的旧分片上传接口已被百度下线（>20MB 文件全部失败），
# 实际执行 bypyx。保留这个文件名，是为了让已存在的宝塔计划任务不用改。
#
# 用法与原脚本一致：
#   ./bypy-sync.sh             跑全部任务
#   ./bypy-sync.sh -t immich   只跑一个任务
#   ./bypy-sync.sh -n          演练
#   ./bypy-sync.sh -l          列出任务

set -u

BYPYX="${BYPYX_BIN:-/opt/bypy-venv/bin/bypyx}"

if [ ! -x "${BYPYX}" ]; then
	echo "找不到 bypyx：${BYPYX}" >&2
	echo "请先按 README 安装：/opt/bypy-venv/bin/pip install -e /www/wwwroot/shells/bypy-sync/bypyx-src" >&2
	exit 2
fi

exec "${BYPYX}" sync "$@"
