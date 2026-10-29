#!/bin/bash
# bypyx 新机一键安装（不需要克隆仓库）
# 用法：curl -fsSL https://raw.githubusercontent.com/fee233/bypy/master/bypyx/deploy/install.sh | bash
#   或下载后：bash install.sh [/可选/安装路径]
set -euo pipefail

VENV="${1:-/opt/bypy-venv}"
CONF_DIR="/www/wwwroot/shells/bypy-sync"
REPO="https://github.com/fee233/bypy.git"

echo "==> 1/5 Python venv"
if [ ! -x "${VENV}/bin/pip" ]; then
    command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
    python3 -m venv "${VENV}" 2>/dev/null || {
        echo "venv 创建失败，多数是缺 python3-venv：apt install -y python3-venv"
        exit 1
    }
fi
PIP="${VENV}/bin/pip"

echo "==> 2/5 安装 bypy（提供 app 凭据与授权流程）+ bypyx（同步引擎）"
"${PIP}" install --quiet --upgrade pip
"${PIP}" install --quiet setuptools wheel
"${PIP}" install --quiet bypy
# 直接从 GitHub 安装，不要求本机有 git、也不需要克隆仓库。
# GitHub 访问不稳时可重试，或改用镜像：
#   ${PIP} install "bypyx @ git+https://ghproxy.net/${REPO}#subdirectory=bypyx"
"${PIP}" install "bypyx @ git+${REPO}#subdirectory=bypyx"

echo "==> 3/5 配置目录"
mkdir -p "${CONF_DIR}" /var/log/bypy-sync
if [ ! -f "${CONF_DIR}/bypy-sync.conf" ]; then
    cat > "${CONF_DIR}/bypy-sync.conf" <<'EOF'
LOG_DIR=/var/log/bypy-sync
LOG_KEEP_DAYS=7
RETRY=5
WORKERS=8
SLICE=4MB
TASK_TIMEOUT=0
LOCK_FILE=/tmp/bypy-sync.lock
VERBOSE=0

# 名称|方向(up=本地到网盘,down=网盘到本地)|本地目录|远端目录(相对/apps/bypy)|删除多余(1=镜像)|文件过滤正则
TASKS=(
  "backup|up|/www/backup|bt_backup|0|"
)
EOF
    echo "    已生成示例配置：${CONF_DIR}/bypy-sync.conf （请编辑 TASKS）"
fi

echo "==> 4/5 验证安装"
"${VENV}/bin/bypyx" -V

echo "==> 5/5 授权状态"
if [ -f "${HOME}/.bypy/bypy.json" ]; then
    echo "    已有授权文件 ~/.bypy/bypy.json，直接可用："
    "${VENV}/bin/bypyx" whoami || true
else
    cat <<'EOF'
    未授权。两种方式任选：
    A) 从旧机拷 token（免重新登录）：scp 旧机:~/.bypy/bypy.json ~/.bypy/
    B) 现场授权：${VENV}/bin/bypy info   → 浏览器打开链接 → 回填 code
EOF
fi

cat <<EOF

完成。后续步骤：
  1. 编辑 ${CONF_DIR}/bypy-sync.conf，填好 TASKS
  2. 演练：   ${VENV}/bin/bypyx sync -n
  3. 首次全量（防断连）：
     cd ${CONF_DIR} && systemd-run --unit=bypyx-sync ${VENV}/bin/bypyx sync
     journalctl -u bypyx-sync -f
  4. 宝塔计划任务一行：${VENV}/bin/bypyx sync
EOF
