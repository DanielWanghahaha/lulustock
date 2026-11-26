#!/bin/bash
# Stock Agent 一键更新：拉取最新代码 → 依赖 → 迁移 → 重启 → 健康检查
set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> 1. 拉取最新代码"
OLD_HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
git pull origin master
NEW_HEAD=$(git rev-parse --short HEAD)
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  echo "已是最新版本（$NEW_HEAD），仅执行依赖与迁移检查"
fi

echo "==> 2. 同步依赖"
.venv/bin/pip install -r requirements.txt -q -i https://mirrors.aliyun.com/pypi/simple/

echo "==> 3. 数据库迁移"
.venv/bin/alembic upgrade head

echo "==> 4. 重启服务"
systemctl restart stock-agent

echo "==> 5. 健康检查（等待启动）"
sleep 8
if ! systemctl is-active --quiet stock-agent; then
  echo "❌ 服务启动失败，最近日志："
  tail -20 logs/app.log
  exit 1
fi
if tail -20 logs/app.log | grep -q "Application started"; then
  echo "✅ 更新完成：$OLD_HEAD → $NEW_HEAD，服务运行中"
else
  echo "⚠️ 服务 active 但日志未见启动标志，请人工检查："
  tail -10 logs/app.log
fi
