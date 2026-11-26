#!/bin/bash
# Stock Agent 部署脚本（在服务器项目目录执行）
set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> 1. 检查 .env"
if [ ! -f .env ]; then
  echo "❌ 缺少 .env，请先复制 .env.production 为 .env 并填写后重试"
  exit 1
fi

echo "==> 2. 检查 Telegram 连通性（大陆 IP 会失败）"
if curl -sS -m 8 https://api.telegram.org >/dev/null 2>&1; then
  echo "✅ api.telegram.org 可达"
else
  echo "❌ api.telegram.org 不可达！大陆服务器无法直连 Telegram，请使用香港/海外服务器或配置代理"
  exit 1
fi

echo "==> 3. 创建虚拟环境并安装依赖"
PY="${PY:-python3}"
$PY -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ -q

echo "==> 4. 数据库迁移"
.venv/bin/alembic upgrade head

echo "==> 5. 验证应用可导入"
.venv/bin/python -c "import app.main; print('import OK')"

echo "==> 6. 创建日志目录"
mkdir -p logs

echo "✅ 部署准备完成。启动方式二选一："
echo "  A. systemd:  sudo cp deploy/stock-agent.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now stock-agent"
echo "  B. 宝塔 Python项目管理器（见 deploy/BT-GUIDE.md）"
