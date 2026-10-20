# 快速开始指南

## 环境要求

- Python 3.12+
- Docker & Docker Compose（可选，用于生产部署）
- uv（Python 包管理器）

## 本地开发

### 1. 安装依赖

```bash
# 克隆项目
git clone <repo-url>
cd gpt-subscribe-studio

# 使用 uv 安装所有依赖
uv sync --python 3.12
```

### 2. 配置环境

```bash
# 创建配置文件
cp deploy/.env.example deploy/.env

# 编辑配置（可选）
vim deploy/.env
```

关键配置项：

```env
# 数据库路径
GSS_DB_PATH=data/gss.db

# 服务绑定地址（默认只监听 localhost）
GSS_BIND_HOST=127.0.0.1
GSS_BIND_PORT=8080

# 促销模式（默认关闭，需要显式开启）
GSS_ENABLE_PROMO_MODE=false

# 上游配置（可选，未配置时使用 Fake adapter）
UPSTREAM_GPT_REGISTER_TOOL_PATH=/path/to/GPT-Register-Tool
UPSTREAM_ZKKY_PATH=/path/to/zkky
```

### 3. 运行测试

```bash
# 运行所有测试（包括单元测试、契约测试、集成测试）
uv run --python 3.12 --with pytest pytest -v

# 只运行单元测试
uv run --python 3.12 --with pytest pytest tests/unit/ -v

# 只运行集成测试
uv run --python 3.12 --with pytest pytest tests/integration/ -v
```

### 4. 启动 API 服务

```bash
# 进入 API 服务目录
cd services/api

# 启动开发服务器
uv run --python 3.12 uvicorn gss_api.main:app --reload --host 127.0.0.1 --port 8080
```

访问 http://127.0.0.1:8080/docs 查看 API 文档。

### 5. 启动 Orchestrator Worker

在另一个终端：

```bash
# 进入 orchestrator 目录
cd services/orchestrator

# 启动 worker（测试模式，使用 Fake adapter）
uv run --python 3.12 python -m gss_orchestrator.worker --fake --poll-interval 5

# 或者使用真实 adapter（需要配置上游路径）
uv run --python 3.12 python -m gss_orchestrator.worker --poll-interval 5
```

Worker 会轮询数据库中的待处理任务并驱动状态机推进。

## 使用 API

### 创建订单

```bash
# 创建一个测试订单
curl -X POST http://127.0.0.1:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "plan": "plus",
    "billing_country": "US",
    "billing_mode": "standard",
    "card_source": "customer",
    "account_source": "customer_session"
  }'
```

响应示例：

```json
{
  "id": "ord_abc123",
  "status": "created",
  "plan": "plus",
  "billing_country": "US",
  "billing_mode": "standard",
  "created_at": "2026-10-09T12:00:00Z"
}
```

### 查询订单状态

```bash
curl http://127.0.0.1:8080/api/orders/ord_abc123
```

### 查询 Plus 活动资格

```bash
curl -X POST http://127.0.0.1:8080/api/promo/eligibility \
  -H "Content-Type: application/json" \
  -d '{"session":"<session-json>"}'
```

如果返回 `plus_eligible=true`，使用返回的 `campaign_id` 创建订单：

```json
{
  "plan": "plus",
  "billing_country": "US",
  "session": "<session-json>",
  "promo_campaign_id": "<campaign_id>",
  "mode": {
    "account_source": "customer_session",
    "card_source": "customer",
    "billing_mode": "promo",
    "risk_acknowledged": true
  }
}
```

### 列出所有订单

```bash
curl http://127.0.0.1:8080/api/orders
```

## 测试模式（Fake Adapter）

当没有配置上游路径时，系统会自动使用 Fake adapter 进行测试：

- **FakeCheckoutAdapter**：生成假的结账链接
- **FakePaymentAdapter**：模拟支付成功
- **FakeAccountAdapter**：模拟订阅已激活

这样可以在不依赖真实上游服务的情况下测试完整流程。

## 生产部署（Docker Compose）

### 1. 构建镜像

```bash
cd deploy
docker compose build
```

### 2. 启动服务

```bash
# 启动所有服务
docker compose up -d

# 查看日志
docker compose logs -f

# 查看服务状态
docker compose ps
```

### 3. 健康检查

```bash
curl http://127.0.0.1:8080/api/health
```

### 4. 停止服务

```bash
docker compose down
```

## 核心工作流

### User Story 1: 客户提供 session，客户提供卡

1. **客户准备**：
   - 导出 ChatGPT session token
   - 准备支付卡信息

2. **创建订单**：
   ```bash
   POST /api/orders
   {
     "plan": "plus",
     "billing_country": "US",
     "billing_mode": "standard",
     "card_source": "customer",
     "account_source": "customer_session"
   }
   ```

3. **Worker 自动处理**：
   - `created` → 生成结账链接 → `pending_checkout`
   - 客户访问链接 → `awaiting_payment`
   - 执行支付 → `paying`
   - 支付成功 → `verifying`
   - 订阅校验通过 → `active`
   - 交付 → `delivered`

4. **客户查询**：
   ```bash
   GET /api/orders/{order_id}
   ```

## 常见问题

### Q: 如何切换到真实 adapter？

A: 在 `.env` 中配置上游路径：

```env
UPSTREAM_GPT_REGISTER_TOOL_PATH=/path/to/GPT-Register-Tool
UPSTREAM_ZKKY_PATH=/path/to/zkky
```

重启服务后会自动使用真实 adapter。

### Q: 如何开启促销模式？

A: 在 `.env` 中设置：

```env
GSS_ENABLE_PROMO_MODE=true
```

然后创建订单时指定 `billing_mode: "promo"`，并且必须设置 `risk_acknowledged: true`。

### Q: 数据存储在哪里？

A: SQLite 数据库文件位于 `data/gss.db`（可通过 `GSS_DB_PATH` 配置）。

### Q: 如何查看详细日志？

A: 所有日志都经过脱敏处理。查看方式：

```bash
# API 服务日志
docker compose logs -f api

# Worker 日志
docker compose logs -f worker
```

### Q: 测试全部通过代表什么？

A: 50 个测试通过意味着：

- ✅ 状态机转换规则正确
- ✅ 模式开关逻辑正确
- ✅ 数据持久化正常
- ✅ Adapter 协议契约满足
- ✅ 脱敏层工作正常
- ✅ 配置加载正确

但**不代表**真实的 ChatGPT API 集成已完成（需要实际测试）。

## 下一步

1. 阅读 [架构文档](architecture.md) 了解系统设计
2. 阅读 [上游集成指南](upstream-integration.md) 了解如何对接真实 API
3. 查看 [任务清单](../specs/001-subscribe-console/tasks.md) 了解开发进度
4. 贡献代码前阅读 [宪法](../constitution.md) 了解设计原则

## 出口代理（每个使用者自己填）

本项目不自带代理。部署后打开页面的「代理设置」标签，粘贴你自己的代理即可，不需要改环境变量或重启：

- 支持 `http://账号:密码@主机:端口`、`账号:密码@主机:端口`、`主机:端口:账号:密码`、`socks5://…`。
- 账号里需要带供应商的地区和 SID 参数，例如 `acct-region-US-sid-abc123`。程序能识别 `region` / `country`、`sid` / `session` 这几种常见写法，替换时保留你供应商原来的键名和大小写。
- 每个订单创建时，程序把地区换成你选的地区、SID 换成新的随机值，同一订单内保持不变；出口地区自动对齐订单所选账单国，无需与 session 导出国家一致。
- 账号里没有地区或 SID 参数时，程序会在末尾追加 `-region-XX`、`-sid-随机值`，请确认你的供应商支持这个写法。
- 代理保存在服务端的配置目录（`GSS_CONFIG_DIR`，容器内为 `/config`），页面和接口都不会回显账号密码。
- 「测试连接」只返回出口 IP 和耗时，是否属于目标地区请自行确认。

## 服务器部署（`deploy/server`）

`deploy/server/compose.yml` 不含任何域名或路径，部署时用环境变量指定：

| 变量 | 说明 |
|---|---|
| `GSS_RELEASE` | 镜像标签（必填），例如构建时间戳 |
| `GSS_PRIVATE_DIR` | 存放 `container-auth.json`（访问密码）和 `proxy-config/` 的目录（必填，不要放进仓库） |
| `GSS_COMPOSE_PROJECT` | compose 项目名，默认 `gpt-subscribe-studio` |
| `GSS_PUBLIC_ORIGIN` | 选填，例如 `https://你的域名`。不填时按「请求 Origin 与 Host 同源」校验，部署到任何域名都能用 |

---

## hCaptcha Solver（可选，Team 协议支付需要）

Stripe 的 checkout 在 confirm 阶段会要求 hCaptcha 验证。产 token 需要一个
独立的浏览器服务，原因见 `tools/hcaptcha-solver/README.md`。

### 为什么不能纯协议

所有 stripe 域的 CSP 都缺 `'unsafe-eval'`：

```
pay.openai.com         ❌
js.stripe.com          ❌
b.stripecdn.com        ❌
m.stripe.network       ❌
newassets.hcaptcha.com ✅   ← 只有这里能跑 wasm
```

hCaptcha 的 wasm 只能在 `newassets.hcaptcha.com` 的 iframe 内实例化。

### 安装

```bash
cd tools/hcaptcha-solver
./install.sh
```

脚本会检查 Node.js / Chrome / Xvfb，缺什么提示什么。

### 启动

```bash
./start.sh
# → hCaptcha solver listening on http://127.0.0.1:8199
```

### 验证

```bash
curl -s http://127.0.0.1:8199/health
# {"ok":true,"browser":"Chrome/155.0.8059.84"}
```

### 接到主服务

```bash
export HCAPTCHA_SOLVER_URL=http://127.0.0.1:8199
```

协议层会自动调用 `POST /solve` 拿 token。

### systemd（生产）

```ini
# /etc/systemd/system/xvfb.service
[Unit]
Description=Xvfb for hCaptcha solver
After=network.target

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/hcaptcha-solver.service
[Unit]
Description=hCaptcha token solver
After=network.target xvfb.service
Requires=xvfb.service

[Service]
Type=simple
WorkingDirectory=/opt/gss/tools/hcaptcha-solver
Environment=DISPLAY=:99
Environment=PORT=8199
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb hcaptcha-solver
```

### Docker（可选）

如果偏好容器：

```yaml
services:
  solver:
    build:
      context: ./tools/hcaptcha-solver
      dockerfile: Dockerfile
    ports:
      - "127.0.0.1:8199:8199"
    shm_size: 1gb
    security_opt:
      - seccomp:unconfined
    restart: unless-stopped
```

需要配套的 `Dockerfile`（自带 Chrome + Xvfb）。
