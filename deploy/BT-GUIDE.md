# 宝塔面板部署指南（Python 项目管理器）

## 前置检查
1. **服务器地域**：Telegram API（api.telegram.org）在大陆被墙，香港/海外地域可直连。
   在服务器终端执行：`curl -m 8 https://api.telegram.org` 能返回内容即可达。
2. **RDS 白名单**：把轻量服务器公网 IP 加入阿里云 RDS 白名单
   （RDS 控制台 → 数据安全性 → 白名单设置 → 添加）。
3. **上传代码**：把 stock-agent.tar.gz 上传到 `/www/wwwroot/` 并解压为 `/www/wwwroot/stock-agent`。
4. **配置 .env**：`cp .env.production .env` 后编辑，填入真实 Token/数据库密码。

## 方式 A：宝塔 Python 项目管理器（推荐，图形化）
1. 宝塔软件商店 → 安装「Python 项目管理器」（选择 Python 3.12 或 3.13 版本安装）。
2. 项目管理器 → 添加项目：
   - 项目名称：stock-agent
   - 路径：/www/wwwroot/stock-agent
   - 启动文件/方式：选择「命令行启动」：`.venv/bin/python -m app.main`
     （先在终端执行 `bash deploy/deploy.sh` 完成 venv 与迁移；若用管理器自建 venv，
      则启动命令改为 `/www/server/pystect/venv/stock-agent_venv/bin/python -m app.main`，以实际 venv 路径为准）
   - Python 版本：3.12+
   - 端口：无需对外端口（Bot 走出站 long polling）
   - 是否安装模块依赖：选「requirements.txt」（管理器会自动建 venv 并安装）
3. 启动后查看「日志」确认：`Application started` 且无 409 冲突。
4. 设置「开机自启」与「进程守护」。

## 方式 B：systemd（更稳）
```
cd /www/wwwroot/stock-agent
bash deploy/deploy.sh
cp deploy/stock-agent.service /etc/systemd/system/
# 检查 service 中 User 与路径是否匹配实际环境
sudo systemctl daemon-reload
sudo systemctl enable --now stock-agent
sudo systemctl status stock-agent
tail -f logs/app.log
```

## 重要提醒
- **同一 Bot Token 只允许一个轮询实例**：部署服务器前必须停掉本地 Mac 上的 Bot，
  否则 Telegram 返回 409 Conflict。
- 阿里云 RDS 本身有自动备份；本项目 `scripts/backup.py` 可用 crontab 每日追加本地备份：
  `0 3 * * * cd /www/wwwroot/stock-agent && .venv/bin/python scripts/backup.py >> logs/backup.log 2>&1`
