# 部署总览

1. 确认服务器地域可访问 api.telegram.org（大陆地域不可行，建议香港轻量）
2. RDS 白名单加入服务器公网 IP
3. 上传 stock-agent.tar.gz 至 /www/wwwroot/ 解压为 stock-agent
4. cp .env.production .env 并填写真实凭证
5. bash deploy/deploy.sh（自动检查连通性/依赖/迁移）
6. 用宝塔 Python 项目管理器或 systemd 启动（详见 deploy/BT-GUIDE.md）
7. 停掉本地 Mac 上的 Bot（避免 409 轮询冲突）
