# 裸机部署（systemd，无 Docker）

> **现状（2026-10-21）**：线上已切换为 **Docker 部署**，见
> [docker/README.md](./docker/README.md)。浏览器路线终止后容器化重新可行
> （唯一遗留的 Node 依赖是 Sentinel 令牌签发，不是浏览器）。
> 本文保留作为裸机方案与回滚参考。


线上就是这套：`/opt/gss` 下的 venv + systemd，没有容器。
本文与服务器实际状态一致，逐步抄就能重建。

## 目录布局

```
/opt/gss/
├── app/                     # 代码 + venv + 前端构建产物
│   ├── app.py               # uvicorn 入口（deploy/server/app.py）
│   ├── login.html
│   ├── requirements.txt
│   ├── .env                 # 运行环境（600）
│   ├── cards.env            # 测试卡（600）
│   ├── packages/
│   ├── services/
│   ├── web/                 # apps/web/dist 的产物
│   └── .venv/
├── config/
│   ├── container-auth.json  # 登录口令（400）
│   └── proxy-gateways.json  # 代理网关（600）
├── data/                    # at_list.json / window_state.json 等
├── logs/                    # api.log / api.err.log / solver.log / window.log
├── scripts/                 # 探针与调度器
└── tools/hcaptcha-solver/   # node 服务 + certs/
```

## 四个 systemd 单元

### gss-api.service

```ini
[Unit]
Description=gss API (non-container)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/gss/app
EnvironmentFile=/opt/gss/app/.env
ExecStart=/opt/gss/app/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 18120
Restart=always
RestartSec=3
StandardOutput=append:/opt/gss/logs/api.log
StandardError=append:/opt/gss/logs/api.err.log

[Install]
WantedBy=multi-user.target
```

### xvfb.service

```ini
[Unit]
Description=Xvfb for hCaptcha solver
After=network.target

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset
Restart=always

[Install]
WantedBy=multi-user.target
```

### gss-solver.service

```ini
[Unit]
Description=hCaptcha token solver
After=network.target xvfb.service
Requires=xvfb.service

[Service]
Type=simple
WorkingDirectory=/opt/gss/tools/hcaptcha-solver
Environment=DISPLAY=:99
Environment=PORT=8199
Environment=ORIGIN_MODE=merchant
Environment=MERCHANT_HOST=b.stripecdn.com
Environment=MERCHANT_PORT=8443
Environment=CERT_DIR=/opt/gss/tools/hcaptcha-solver/certs
Environment=MAX_SESSIONS=6
Environment=SESSION_TTL_MS=600000
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`ORIGIN_MODE=merchant` 让页面真的跑在 `https://b.stripecdn.com`
（自签证书 + `--host-resolver-rules`），challenge iframe 的 host 才与真实浏览器一致。
证书由 solver 首次启动时用 openssl 自动生成到 `certs/`。

### gss-window.service

```ini
[Unit]
Description=hCaptcha window scheduler
After=network.target gss-api.service gss-solver.service

[Service]
Type=simple
WorkingDirectory=/opt/gss/app
EnvironmentFile=/opt/gss/app/.env
EnvironmentFile=/opt/gss/app/cards.env
Environment=RUN_MINUTES=180
Environment=ROUNDS_PER_WINDOW=30
ExecStart=/opt/gss/app/.venv/bin/python /opt/gss/scripts/window_scheduler.py
Restart=always
RestartSec=30
StandardOutput=append:/opt/gss/logs/window.log
StandardError=append:/opt/gss/logs/window.err.log

[Install]
WantedBy=multi-user.target
```

## 反向代理（Caddy）

```caddyfile
<PAY_HOST> {
    encode gzip zstd
    reverse_proxy 127.0.0.1:18120 {
        header_up -Authorization
        flush_interval -1      # SSE 必须关缓冲
    }
}
```

`flush_interval -1` 不能省，否则前端 SSE 订阅（`/api/orders/{id}/events`）会被攒着不推送。

## .env 必填项

```bash
GSS_DATA_DIR=/opt/gss/data
GSS_CONFIG_DIR=/opt/gss/config
GSS_AUTH_FILE=/opt/gss/config/container-auth.json
GSS_LOGIN_FILE=/opt/gss/app/login.html
GSS_WEB_DIR=/opt/gss/app/web
GSS_BIND_HOST=127.0.0.1
GSS_BIND_PORT=18120
GSS_PUBLIC_ORIGIN=https://你的域名

# hCaptcha
HCAPTCHA_SOLVER_URL=http://127.0.0.1:8199
GSS_CAPTCHA_API_KEY=            # 可选，打码兜底；余额为 0 时是 ERROR_ZERO_BALANCE
GSS_CAPTCHA_API_URL=https://api.yescaptcha.com

PYTHONPATH=/opt/gss/app/packages/contracts/src:/opt/gss/app/packages/core/src:\
/opt/gss/app/services/account/src:/opt/gss/app/services/checkout/src:\
/opt/gss/app/services/payment/src:/opt/gss/app/services/api/src:\
/opt/gss/app/services/orchestrator/src
```

## 发布步骤

```bash
# 1 后端（改动了 packages/ services/ 之后）
rsync -az --exclude '.venv' --exclude '__pycache__' \
  packages/ services/ root@HOST:/opt/gss/app/
systemctl restart gss-api

# 2 前端（改动了 apps/web/ 之后）
cd apps/web && npm run build
rsync -az --delete dist/ root@HOST:/opt/gss/app/web/
# 前端是静态文件，不用重启 api

# 3 solver（改动了 tools/hcaptcha-solver/ 之后）
scp tools/hcaptcha-solver/server.js root@HOST:/opt/gss/tools/hcaptcha-solver/
ssh root@HOST 'node --check /opt/gss/tools/hcaptcha-solver/server.js \
  && systemctl restart gss-solver'

# 4 脚本（探针/调度器）
scp scripts/*.py root@HOST:/opt/gss/scripts/
ssh root@HOST 'systemctl restart gss-window'
```

注意：改了 solver 之后必须 `pkill -f "node server.js"` 再 restart，
否则旧进程占着 8199，新进程会 `EADDRINUSE` 静默失败，而 `systemctl is-active`
仍然显示 active（旧进程还活着）。

## 检查

```bash
systemctl is-active gss-api gss-solver gss-window xvfb caddy
curl -s http://127.0.0.1:8199/health        # {"ok":true,"browser":"Chrome/155..."}
curl -s http://127.0.0.1:8199/sessions      # 当前会话池
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18120/login   # 200
ss -tlnp | grep -E '18120|8199|8443|:99'
```

## 首次部署（从零）

```bash
apt-get install -y python3.12 python3.12-venv xvfb nodejs \
  fonts-liberation libgbm1 libnss3 libatk-bridge2.0-0 libgtk-3-0
# Google Chrome
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
  | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update && apt-get install -y google-chrome-stable

mkdir -p /opt/gss/{app,config,data,logs,scripts}
cd /opt/gss/app && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd /opt/gss/tools/hcaptcha-solver && npm install --omit=dev

# 认证口令
printf '{"password":"%s"}' "$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)" \
  > /opt/gss/config/container-auth.json
chmod 400 /opt/gss/config/container-auth.json

# 单元文件写好之后
systemctl daemon-reload
systemctl enable --now xvfb gss-solver gss-api gss-window
```

## 为什么不是 Docker

早期有过 compose 方案（`deploy/docker-compose.yml`、`deploy/server/compose.yml`），已删除。原因：

1. solver 需要 Chrome + Xvfb + GPU 栈，容器里再加一层 shm/seccomp 参数，调试成本高于收益。
2. 探针脚本要频繁改，挂进容器要么重建镜像要么挂卷，直接跑在宿主机上更快。
3. 服务器本来就在跑别的容器项目，Docker daemon 一旦重启会连带影响；systemd 单元互不干扰。

`docker` 在这台机器上仍然装着，属于其他项目，与 gss 无关。
