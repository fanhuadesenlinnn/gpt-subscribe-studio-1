# hCaptcha Token Solver

给 Stripe checkout 使用的 hCaptcha token 求解服务。

## 为什么需要独立服务

Stripe 各域的 CSP 都缺 `'unsafe-eval'`：

| 域 | `script-src` 是否含 `'unsafe-eval'` |
|---|---|
| `pay.openai.com` | ❌ |
| `js.stripe.com` | ❌ |
| `b.stripecdn.com` | ❌ |
| `m.stripe.network` | ❌ |
| **`newassets.hcaptcha.com`** | **✅** |

hCaptcha 的 `hsw` wasm 只能在 `newassets.hcaptcha.com` 的 iframe 内实例化。
在 stripe 域里直接调 `WebAssembly.Module()` 会抛：

```
EvalError: Compiling or instantiating WebAssembly module violates
the following Content Security policy directive because
'unsafe-eval' is not an allowed source of script
```

所以必须用一个中性 origin 加载 `hcaptcha.com/1/api.js`，
由它自己创建 `newassets` iframe，wasm 在 iframe 内正常跑。

## 前置条件

- Node.js ≥ 18
- Google Chrome（`/usr/bin/google-chrome`）或其他 Chromium
- Xvfb（无桌面环境时必需）

### 安装 Xvfb

```bash
# Debian / Ubuntu
sudo apt-get install -y xvfb

# 启动（1920x1080，带 GLX）
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
```

有桌面环境时可跳过 Xvfb，直接 `export DISPLAY=:0`。

## 安装

```bash
cd tools/hcaptcha-solver
npm install
```

## 运行

```bash
export DISPLAY=:99
npm start
# → hCaptcha solver listening on http://127.0.0.1:8199
```

检查：

```bash
curl -s http://127.0.0.1:8199/health
# {"ok":true,"browser":"Chrome/155.0.8059.84"}
```

## 接口

### `POST /solve`

请求：

```json
{
  "sitekey": "24ed0064-62cf-4d42-9960-5dd1a41d4e29",
  "rqdata": "<init 返回的 link_settings.hcaptcha_rqdata>",
  "host": "b.stripecdn.com"
}
```

响应：

```json
{ "token": "P1_eyJhbGciOiJIUzI1NiIs...", "elapsed_ms": 12345 }
```

失败：

```json
{ "error": "execute_timeout" }
```

### `GET /health`

```json
{ "ok": true, "browser": "Chrome/155.0.8059.84" }
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8199` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `CHROME_PATH` | `/usr/bin/google-chrome` | Chrome 可执行文件 |
| `PUPPETEER_PATH` | `puppeteer-core` | puppeteer 模块路径 |
| `HCAPTCHA_SITEKEY` | `24ed0064-...` | 默认 sitekey |
| `HCAPTCHA_HOST` | `b.stripecdn.com` | 默认 host |
| `TOKEN_TIMEOUT_MS` | `90000` | execute 超时 |

## 两个 sitekey

| 用途 | sitekey |
|---|---|
| passive（`init` 的 `link_settings.hcaptcha_site_key`） | `24ed0064-62cf-4d42-9960-5dd1a41d4e29` |
| challenge（`next_action.stripe_js.site_key`） | `c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a` |

请求时传实际的 sitekey，不要写死。

## 产出 token 的结构

与真实浏览器抓包（HAR）对比：

| 字段 | 浏览器 | 本服务 |
|---|---|---|
| 总长 | 3,772 | 3,783–3,810 |
| `pd` | 0 | 0 |
| `exp` | 10 位 | 10 位 |
| `cdata` | 460 | 460 |
| `passkey` | 2,220 | 2,236 |
| `kr` | 8 位 | 8 位 |
| `shard_id` | 9 位 | 9 位 |

## 常见问题

### `EvalError: ... 'unsafe-eval' is not an allowed source`

说明页面 origin 不对。必须用 `http://127.0.0.1:<port>`，
不能是 `b.stripecdn.com` 或 `about:blank`。

### `invalid-data`

`render` 的 `host` 参数与 sitekey 不匹配，或 `rqdata` 缺失。
`host` 通常是 `b.stripecdn.com`。

### `execute_timeout`

- 检查 Xvfb 是否在跑（`ps aux | grep Xvfb`）
- 检查 `DISPLAY` 环境变量
- 网络慢时调大 `TOKEN_TIMEOUT_MS`

### 无头模式可以吗

不行。实测 `headless: 'new'` 下 hCaptcha 的 wasm 更容易失败，
必须 `headless: false` + Xvfb。

## systemd 单元

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

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb hcaptcha-solver
sudo systemctl status hcaptcha-solver
```

## 许可

跟随主项目。
