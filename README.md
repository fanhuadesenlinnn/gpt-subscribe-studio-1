# 直卡协议 / gpt-subscribe-studio

> Stripe Hosted Checkout 的**纯协议**对接实现：不发浏览器、不调打码服务，
> 用 HTTP 直接跑完从结账会话到支付确认的整条链路，并把每一步的开关交给使用者。

跑一单默认耗时 **7 秒**。

---

## 目录

- [现在是什么](#现在是什么)
- [支持的档位](#支持的档位)
- [协议链路](#协议链路)
- [实测数据](#实测数据)
- [快速开始](#快速开始)
- [配置](#配置)
- [API](#api)
- [目录结构](#目录结构)
- [本地开发](#本地开发)
- [已知行为](#已知行为)
- [hCaptcha 探索记录](#hcaptcha-探索记录)
- [未解决的问题](#未解决的问题)
- [免责声明与风险提示](#免责声明与风险提示)
- [仓库安全](#仓库安全)
- [归档](#归档)

---

## 现在是什么

一套自托管的 Stripe 结账协议客户端。前端只做表单与结果展示，所有与 Stripe 的交互都在服务端完成。

覆盖六个订阅档位，但**纯协议链路只适用于 Team**（hosted checkout）。
Plus / Go / Pro 家族走的是 custom checkout，链路不同，见「支持的档位」。

### 链路（Team）

```
粘贴 ChatGPT accessToken
  → 建结账会话（发 openai /pay，取 cs_live_）
  → Stripe 协议链路（见下一节）
  → 结果：成功 / 拒卡 + 原因 / 命中风控挑战
```

### 不做什么

- **服务端不启动浏览器**。链路里没有任何 Chrome / Playwright / Puppeteer；
  Stripe 输入框模式用的是你自己浏览器里的 Stripe.js。
- **不调用打码服务**。命中 hCaptcha `intent_confirmation_challenge` 时直接判定放弃本单。
- **不落库卡号**。卡号只在单次请求的内存里使用，不写日志、不入库。

---

## 支持的档位

六个档位分成两族，**结账类型不同，支付链路也完全不同**。

| 档位 | 代码 | 挂牌价 | 结账类型 | 会话 id | 支付入口 |
|---|---|---|---|---|---|
| Plus | `plus` | $20 / 月 | custom | `oaics_` / `cs_live_` | **`/pay-protocol` 纯协议** |
| Go | `go` | $8 / 月 | custom | `oaics_` / `cs_live_` | **`/pay-protocol` 纯协议** |
| Pro Lite | `pro_lite` | $100 / 月 | custom | `oaics_` / `cs_live_` | **`/pay-protocol` 纯协议** |
| Pro | `pro` | $200 / 月 | custom | `oaics_` / `cs_live_` | **`/pay-protocol` 纯协议** |
| Pro Max | `pro_max` | $500 / 月 | custom | `oaics_` / `cs_live_` | **`/pay-protocol` 纯协议** |
| Team | `team` | $25 / 席 / 月 | hosted | `cs_live_` | **`/pay-protocol` 纯协议** |

### Plus 家族（custom checkout）

- 结账地址是 `https://chatgpt.com/checkout/…`，会话 id 前缀 `oaics_`
- **没有** `publishable_key`
- 支付要分两步：先向 ChatGPT 的 `/payments/checkout/confirm` 提交确认，
  拿到 PaymentIntent 后再向 Stripe 确认这个 intent
- 确认需要一个 confirmation token（`ctoken_`），由 Stripe Elements 在浏览器端产出

### Team（hosted checkout）

- 结账地址是 `https://pay.openai.com/c/pay/cs_live_…`，会话 id 前缀 `cs_live_`
- 有 `publishable_key`
- 支付直接打 Stripe 的 `payment_pages/{cs}/confirm`，不需要经过 ChatGPT 内部接口
- 这是本仓库纯协议链路的适用范围

### 两者在参数上的差异

| 项目 | Plus 家族 | Team |
|---|---|---|
| 账单国家 | 必须与建单地区一致，不一致直接报 `billing_country_mismatch` | 可独立选择，仅需在支持列表内 |
| 席位数量 | 不涉及 | 2 席起，可配 |
| 月付 / 年付 | 固定 | 可选 |
| 工作空间名 | 不涉及 | 可填 |
| 优惠码 | 取决于 `GSS_ENABLE_PROMO` | 同 |

### 支持的国家 / 地区（25 个）

```
US PH CL EG JP GB TR BR MX IN ID VN TH MY SG KR AU CA DE NL PL AR SA AE ZA
```

每个地区绑定币种，建单时决定结账币种。完整表在 `packages/core/src/gss_core/countries.py`。

### 实现状态

两族都走 `/pay-protocol`，服务端按订单的 `checkout_kind` 自动分流：

```
checkout_kind = hosted  → StripeProtocol   （Team，payment_pages/{cs}/confirm）
checkout_kind = custom  → PlusProtocol     （Plus 家族，ChatGPT confirm + PaymentIntent）
```

Plus 家族的会话 id 有两种形态，两种都支持：

- `oaics_…` —— ChatGPT 内部结账会话
- `cs_live_…` —— Stripe Custom Checkout 会话（此时额外走一次 finalize 取 PaymentIntent）

### Plus 的协议链路

默认按 2026-10-29 HAR 逐请求复刻（`GSS_PLUS_ENGINE=har`，详见
[docs/plus-har-protocol.md](docs/plus-har-protocol.md)）：

```
① CES 遥测 + mint_sentinel        挂 openai-sentinel-token / -so-token，/sentinel/ping
② GET  /backend-api/payments/checkout/{processor}/{id}
③ GET  /v1/elements/sessions      Elements 启动（税费前金额）
④ m.stripe.com/6 ×4 + r.stripe.com/b  设备指纹 + SDK 事件流（设备按账号持久化）
⑤ POST /v1/consumers/sessions/lookup ×2 + GET /edge-internal/card-metadata
⑥ POST /backend-api/payments/checkout/taxes + update_tax_id
⑦ GET  /v1/elements/sessions      税费后金额
⑧ POST /v1/confirmation_tokens    卡内联 + radar token + guid/muid/sid
⑨ POST /backend-api/payments/checkout/confirm（四字段）
⑩ POST /v1/payment_intents/{id}/confirm（同一 ctoken + return_url）
⑪ GET 原结账 → complete/paid → POST /checkout_session → GET /subscriptions
```

旧链路（`/v1/payment_methods` 造 `pm_`）保留在 `GSS_PLUS_ENGINE=legacy`。

### 两种提交方式（界面可切换，默认协议）

Plus / Pro 家族的界面上有「协议提交」与「Stripe 输入框」两个选项：

| 方式 | 卡号经过 | 取 pm_ 的方式 | 适用 |
|---|---|---|---|
| **协议提交**（默认） | 本服务 → Stripe API | 服务端直传 `confirmation_tokens`（HAR 复刻） | 批量、无人值守 |
| **Stripe 输入框** | 只在浏览器 → Stripe iframe | 浏览器 Stripe.js 产出 `pm_` | 指纹与 Radar 信号最接近真人 |

两种方式在 `pm_` 之后的服务端链路完全相同：

```
pm_ → ctoken_ → ChatGPT confirm → PaymentIntent → Stripe confirm
```

API 层通过二选一的字段区分：

```json
{ "card": {...}, "billing": {...} }              // 协议提交
{ "payment_method_id": "pm_…", "billing": {...} } // Stripe 输入框
```

两者都要求 `billing`，且都会走同一套税费同步与保存选择配对逻辑。

**注意**：用 publishable key 造 `pm_` 时必须带 `client_attribution_metadata`，
否则 Stripe 返回 `This integration surface is unsupported for publishable key tokenization`。

两个容易踩的点：

- **保存选择必须配对**：ctoken 的 `setup_future_usage` 与 confirm 的
  `save_payment_method_for_reuse` 不一致时，上游返回
  `Card save choice does not match confirmation token`。
  代码按 `(None, None)` → `(off_session, true)` → `(on_session, true)` 依次尝试。
- **pm 要声明 `allow_redisplay=always`**：订阅需要保存卡片复用，不声明时
  即使保存选择配对也会被拒。


---

## 协议链路

以下链路适用于 **Team（hosted checkout）**。

### 默认模式（与批量脚本一致）

```
② init              GET  payment_pages/{cs}/init        取金额 / sitekey / rqdata
⑥ payment_methods   POST /v1/payment_methods            提交卡号换 pm_
⑦ confirm           POST payment_pages/{cs}/confirm     402 拒卡 / 200 挑战或成功
⑧ poll              GET  payment_pages/{cs}             只有未放弃时才轮询
```

### 可选环节（网页上勾选，默认全关）

| 开关 | 做什么 | 打开后的影响 |
|---|---|---|
| `fingerprint` | `m.stripe.com/6` × 4 注册 guid/muid/sid，再拉 `allowed_origins` | 多 5 次请求；关闭时用随机 uuid 顶替 |
| `dwell` | init 后停留 10–16 秒，模拟阅读 | 耗时 +10~16s |
| `elements` | `elements/sessions` 初始化 | 多一次会话协商 |
| `tax_region` | 提交 `tax_region[country]` | Stripe 按国家重算税额，金额可能变化 |

开关在 API 里对应 `steps: { fingerprint, dwell, elements, tax_region }`，全部默认 `false`。
关掉指纹时 `guid/muid/sid` 用随机 uuid 生成，与批量脚本逻辑一致。

### 命中挑战就放弃

`confirm` 返回 `next_action.type = intent_confirmation_challenge` 时：

```
⑦b 命中 challenge site_key=c7faac4c
   已放弃，跳过轮询
判定 challenge_required
```

挑战令牌必须在**持有该 `rqdata` 的同一会话**里签出，第三方求解在结构上无效，
所以纯协议路径不做求解尝试，直接把这一单标记为 `challenge_required` 交回上层。

---

## 实测数据

捕获自本机对生产 Stripe 环境的真实调用。

### 18 张卡批量实测

| 结果 | 张数 | 说明 |
|---|---|---|
| 走到 confirm | 12 | 其余 6 张在建单/地址阶段就失败，未到支付 |
| ├ **直接拒卡** `card_declined` | **4** | **占走到 confirm 的 33%**，是明确结论 |
| └ 命中 hCaptcha 挑战 | 8 | 本单作废，无结论 |
| 建单失败 / 地址不符 / 网络错误 | 6 | 非支付层原因 |

**33% 的直接拒卡率是这条链路的有效产出** —— 每 3 张能走到 confirm 的卡里，
就有 1 张能拿到明确结论（`fraudulent` 等拒卡原因），不需要过挑战。

按请求计的挑战率是另一个口径：这 12 张卡在反复重试中累计 36 次 confirm，
其中约 94% 命中挑战。两个数字都对，分母不同。

### Plus 家族批量实测（18 张卡池）

全部走 `plan=plus`、账单地址 US、标准模式：

| 结果 | 张数 | 说明 |
|---|---|---|
| **拒卡** | **18 / 18** | 全部拿到 `card_declined` |
| ├ `fraudulent` | 13 | 发卡行判定风险交易 |
| ├ `generic_decline` | 1 | 发卡行未说明原因 |
| └ ChatGPT 侧拒绝 | 4 | 未到发卡行，无 Stripe 拒卡码 |

**拒卡率 100%。** 单次约 14 秒。

拒卡码的分布有信息量：13 张落在 `fraudulent`，说明这套卡在 Plus（$20/月）
这个价位上基本都被发卡行直接判风险，与是否过挑战无关 —— 纯协议链路
不需要通过 hCaptcha 就能拿到这个结论。

### 代理出口的两个坑

- **地区必须显式对齐**：网关配置里的用户名是 `-region-JP-`，
  不调 `align_proxy_region(..., "US")` 会抽到日本出口，
  Stripe 直接回 `billing country must match`。
- **SID 固定 = 坏出口会一直坏**：代理抽到坏 IP 时表现为
  `SSL: UNEXPECTED_EOF_WHILE_READING` 或 `TLS connect error`。
  `PlusProtocol.pay_from_url()` 遇到这类错误会轮换 SID 重试，最多 3 次。
  实测该代理约 17% 的连接会失败。

### 耗时

| 项目 | 数值 |
|---|---|
| Team 默认精简链路 | **7 秒** |
| Team 全环节打开 | 35 秒 |
| Plus 家族（custom 链） | 约 14 秒 |

### 两个 hCaptcha sitekey

| sitekey | 场景 | 纯协议下 |
|---|---|---|
| `24ed0064-…` | Stripe passive（invisible） | 跳过不提交 |
| `c7faac4c-…` | `intent_confirmation_challenge` | 命中即放弃 |

### 挑战率与这些因素无关

已排除的变量：TLS 指纹（curl_cffi Chrome vs httpx 默认）、UA、`rv_timestamp`、
passive token、指纹注册、elements 会话、停留时长、tax_region、出口国家与 ISP。
逐个调整都不改变命中率。

---

## 快速开始

### Docker（推荐）

```bash
# 1) 准备宿主机目录
mkdir -p /opt/gss/{data,config}

# 2) 放配置（见「配置」一节）
#    /opt/gss/config/container-auth.json   后台登录口令
#    /opt/gss/config/proxy-gateways.json   出口网关
#    /opt/gss/app/.env                     环境变量

# 3) 构建并启动
cd /opt/gss/src
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 4) 验证
docker ps --filter name=gss-api --format '{{.Status}}'   # Up (healthy)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18120/   # 303
```

镜像用多阶段构建：`node:20-bookworm-slim` 提供 node 二进制与 `undici`，
最终镜像是干净的 `python:3.12-slim`。

**Node 是必需的**：`sentinel.py` 通过 subprocess 调 `sentinel_bridge.js`
签发 ChatGPT 的 Sentinel 令牌。那不是浏览器，是一段纯 JS（浏览器 shim +
混淆 sdk.js + PoW）。缺 Node 时建单会返回：

```json
{"code": "checkout_upstream_error", "message": "无法签发 ChatGPT 风控令牌（Sentinel）。…"}
```

详细部署说明见 [deploy/docker/README.md](./deploy/docker/README.md)。

### 本地开发

```bash
# 后端：仓库根目录执行，靠 PYTHONPATH 暴露各子包的 src 布局
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/core/src:$PWD/services/account/src:$PWD/services/checkout/src:$PWD/services/payment/src:$PWD/services/api/src:$PWD/services/orchestrator/src"
python -m venv .venv && .venv/bin/pip install -r deploy/server/requirements.txt
cd deploy/server && ../../.venv/bin/python -m uvicorn app:app --reload --port 18120

# 前端
pnpm --dir apps/web install
pnpm --dir apps/web dev
```

---

## 配置

环境变量（`.env`，不入库）：

| 变量 | 说明 |
|---|---|
| `PYTHONPATH` | 六个 `src` 目录，容器内由镜像覆盖 |
| `GSS_AUTH_FILE` | 后台口令文件路径 |
| `GSS_DATA_DIR` | 运行数据目录（at_list 等） |
| `GSS_CONFIG_DIR` | 配置文件目录 |
| `GSS_PUBLIC_ORIGIN` | 对外 Origin，例如 `https://<PAY_HOST>` |
| `GSS_BIND_HOST` / `GSS_BIND_PORT` | 监听地址，容器内为 `0.0.0.0:18120` |
| `GSS_WEB_DIR` / `GSS_LOGIN_FILE` | 前端产物与登录页路径 |
| `GSS_USE_FAKE` | 离线 Fake 模式（不发真实请求） |
| `GSS_ENABLE_PROMO` | 是否开放 promo 计费模式，默认关闭 |
| `GSS_CAPTCHA_API_KEY` | 打码服务密钥；纯协议路径不使用，留空即可 |
| `GSS_PLUS_ENGINE` | `har`（默认，2026-10-29 HAR 逐请求复刻）/ `legacy`（旧 pm_ 链路） |
| `GSS_HAR_TELEMETRY` | `full`（默认，m.stripe.com/6 + r.stripe.com/b + js.stripe.com/m.stripe.network 资源 + CES）/ `off` |
| `GSS_HAR_TELEMETRY_BUDGET` | 遥测时间预算（秒），默认 `120`；超时后丢弃剩余事件，不阻塞支付 |
| `GSS_HAR_CAPTURE` | 抓包落盘路径（JSONL），留空不抓 |
| `GSS_DEVICE_OS` | 设备系统，留空跟随本机（`sys.platform`）；可强制 `linux` / `macos` / `windows` |
| `GSS_DEVICE_FINGERPRINT` | 本机指纹 JSON（`scripts/fingerprint_probe.js` 产出），canvas/audio/cores/时区取自真机 |
| `GSS_DEVICE_STORE` | 按账号持久化的设备库，默认 `$GSS_DATA_DIR/device_store.json` |
| `GSS_SOLVER_PROXY` | 求解器专用出口（HTTP 桥），留空则用订单代理 |
| `GSS_PROXY_SID` | 钉住某个 sticky 会话（调试 / 让求解器与支付同 IP） |
| `GSS_LLM_SOLVER_URL` | hCaptcha passive 求解器地址，产出 `P1_…` radar token |

`STRIPE_PUBLISHABLE_KEY` 只给旧引擎（`GSS_PLUS_ENGINE=legacy`）用；HAR 引擎从结账页里读 publishable key，代码里不再存任何 key。

### 账单地址与邮箱

- **地址**：从美国五个全州无销售税的州随机生成（AK / DE / MT / NH / OR），
  街道、城市、州、邮编都可在界面修改。
- **邮箱**：从 ChatGPT accessToken 的 JWT 载荷 `https://api.openai.com/profile.email`
  解析后自动填入，用户可直接改。界面上会标明当前值来自解析还是手改。

不固定地址是为了避免每次同一地址；落在免税州是为了让应付金额恒定 ——
`confirm` 的 `expected_amount` 必须等于会话当前总额，地址一变动金额就变。

---

## API

均在 `/api` 前缀下，需要登录态 `__Host-gss_session`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/orders` | 建单（plan / billing_country / session / 席位等） |
| `GET` | `/orders/{id}` | 查单，含 `session_email` |
| `POST` | `/orders/{id}/pay-protocol` | **协议支付**（本项目主路径） |
| `POST` | `/orders/{id}/pay` | 编排管线支付 |
| `POST` | `/orders/{id}/verify` | 查询订阅是否生效 |
| `GET` | `/orders/{id}/stream` | SSE 实时状态 |
| `POST` | `/orders/{id}/team/quote` | Team 报价 |
| `POST` | `/orders/{id}/team/prepare` | Team 支付前准备 |
| `POST` | `/orders/{id}/team/poll` | Team 轮询 |
| `POST` | `/promo/eligibility` | promo 资格查询 |
| `GET` | `/settings/proxies` | 出口网关配置 |
| `POST` | `/settings/proxies/parse` | 解析代理串 |
| `POST` | `/settings/proxies/{role}/test` | 测试出口连通性 |

`pay-protocol` 请求体：

```json
{
  "card": { "number": "…", "exp_month": "…", "exp_year": "…", "cvc": "…" },
  "billing": {
    "name": "…", "email": "…", "line1": "…",
    "city": "…", "state": "…", "postal_code": "…", "country": "US"
  },
  "proxy": "http://user:pass@host:port",
  "steps": { "fingerprint": false, "dwell": false, "elements": false, "tax_region": false }
}
```

响应：

```json
{
  "state": "succeeded | card_declined | challenge_required | unknown",
  "code": null,
  "decline_code": null,
  "message": null,
  "intent_status": "requires_action",
  "charge_id": null,
  "payment_method_id": null
}
```

---

## 目录结构

```
packages/
  contracts/      数据模型与枚举
  core/           Stripe 协议客户端、Sentinel 桥、会话解析、代理
services/
  api/            FastAPI 路由与序列化
  checkout/       结账会话适配器
  payment/        支付适配器
  orchestrator/   管线编排与状态机
  account/        账号侧逻辑
apps/web/         React + Mantine 前端
deploy/
  docker/         Dockerfile / compose / 部署文档
  server/         容器入口 app.py 与登录页
scripts/          批量跑卡、探针、窗口调度
docs/             协议逆向与探索记录
attic/            已归档的浏览器路线（不参与构建）
```

---

## 本地开发

```bash
# 类型检查
cd apps/web && ./node_modules/.bin/tsc --noEmit -p tsconfig.json

# 构建前端
./node_modules/.bin/vite build

# 语法检查全部 Python
python3 -c "import ast,glob;[ast.parse(open(f).read()) for f in glob.glob('**/*.py',recursive=True)]"
```

批量跑卡（服务端）：

```bash
cd /opt/gss/app && set -a && . .env && set +a
printf '卡号|月/年|CVC||||US\n' | /opt/gss/app/.venv/bin/python /opt/gss/scripts/run_cards.py
```

脚本与网页默认走同一条链路（`init → payment_methods → confirm`）。

---

## 已知行为

**挑战不可绕过。** 命中 `intent_confirmation_challenge` 时这一单就结束了，
只能丢弃重试。已排除的变量清单与实测过程见「hCaptcha 探索记录」，
还没弄清的部分见「未解决的问题」。

**金额可能随税额变化。** 勾选 `tax_region` 后 Stripe 会按账单国家重算总额，
后端会同步刷新 `expected_amount`。落在免税州时不会发生。

**Node 是运行期依赖。** 见「快速开始」。

---

## hCaptcha 探索记录

这一节只写有实测支撑的结论。完整过程见
[docs/exploration-log-browser-route.md](./docs/exploration-log-browser-route.md)
与 [docs/hcaptcha-direct-solve.md](./docs/hcaptcha-direct-solve.md)。

### 两个 sitekey，性质完全不同

| sitekey | 来源 | 形态 | 纯协议下的处理 |
|---|---|---|---|
| `24ed0064-62cf-4d42-9960-5dd1a41d4e29` | init 响应的 `link_settings.hcaptcha_site_key` | passive（invisible），多数情况直接放行 | 跳过，不提交 token |
| `c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a` | confirm 响应的 `next_action.use_stripe_sdk.stripe_js.site_key` | `intent_confirmation_challenge`，几乎必然是图片任务 | 命中即放弃本单 |

`c7faac4c` 的挑战**每次都是九宫格或拖拽**，没有出现过 invisible 直接通过的样本。

### 试过的路径与结果

| 路线 | 做法 | 结果 |
|---|---|---|
| 第三方打码（无代理） | YesCaptcha `HCaptchaTaskProxyless` | 拿得到 token，但提交给 Stripe 不认 |
| 第三方打码（带代理） | YesCaptcha `HCaptchaTask` + 同出口 | 同上，仍不认 |
| 同步分类 | YesCaptcha `HCaptchaClassification` | 能识别图片坐标，但对推进支付无帮助 |
| 无头 Chrome 求解 | 自己驱动 challenge iframe | 上下文评分过低，token 被拒 |
| Camoufox 求解 | 真实 Firefox + 指纹修补 | `mediump 23/127/127`（Chrome 只有 `10/15/15`），仍不保证通过 |
| 手工题面切分 | 对 canvas 截图按九宫格切 | 切不准，因为截的是放大品 |
| 直接放弃 | 命中即结束本单 | **当前采用** |

### 关键发现一：挑战令牌与会话绑定

挑战令牌必须在**持有该次 `rqdata` 的同一会话**里签出。跨会话、跨 IP、跨浏览器拿到的
token 提交上去一律无效。这条直接判了第三方打码的死刑 —— 不是打码质量不够，
而是结构性不可能：解题进程不在那个会话里。

### 关键发现二：canvas 上是放大品，原图在 `drawImage`

自己截图切九宫格一直切不准，原因是 canvas 是 1000×940 的**放大品**，上方还有约 240px 黑边。
拦截 `CanvasRenderingContext2D.prototype.drawImage` 后拿到了真正送进 canvas 的位图：

```
网格题    1 张 480×320 合成图（3 行 3 列，严丝合缝无缝隙）
拖拽题    1 张 480×320 背景 + 2 张 85×85 可拖小块
```

所以正确的切法是按 3×3 均分 480×320，而不是对着放大后的 canvas 量坐标。

### 关键发现三：`rv_timestamp` 生成不出来

confirm 请求里带一个 `rv_timestamp`：**212 字符**，字符集 43 个符号
（`` #$&;<=>?CDLMNOPQRTUXY[\]^_`bdenoqrstuvwxyz{ ``）。

抓了 605 条 HAR 记录里加载的**全部 131 个 JS 文件**：

- 只有 `checkout-app-init` 与 `controller` 两个 bundle 出现这个字段，且都只是透传
- 按字符集反查：**0 个文件命中**
- 按「长度 212 附近 + `Math.random` / `fromCharCode` 组合」反查：**0 个文件命中**
- init 响应里也不返回这个值

结论：生成逻辑不在客户端加载的代码里，很可能是服务端下发或 WASM 内部产物。

### YesCaptcha 接口踩过的坑

| 现象 | 原因 |
|---|---|
| `ERROR_PROXY_READ_TIMEOUT` | 代理字段名写成了 `proxyUserName`，正确是 `proxyLogin` |
| 报未知参数 | 没有 `isEnterprise` 字段；`rqdata` 是顶层字段，不在 `task` 里 |
| 空结果也扣费 | 用 `HCaptchaClassification` 时，返回空结果不扣点 |
| 计费差异 | `HCaptchaTask` 15 点（带代理）/ `HCaptchaTaskProxyless` 30 点 |

这轮探索在打码上花了约 309 点，其中约 60 点浪费在字段名写错和空探测上。

---

## 未解决的问题

按影响排序，每条都写了「已知」与「缺什么」。

**1. 挑战率降不下来（按请求计约 94%，36 次 confirm；按卡计 8/12）**

已知：逐个调整 TLS 指纹、UA、`rv_timestamp`、passive token、指纹注册、elements 会话、
停留时长、tax_region、出口国家与 ISP，命中率都不变。
缺：Stripe 判定 `intent_confirmation_challenge` 的实际特征是什么。
目前只能观测到「有些卡的 BIN 直接 402 拒卡、不弹挑战」，但触发差异的原因没有解释。

**2. `rv_timestamp` 生成算法未知**

已知：长度 212、43 字符集、客户端只做透传、服务端不返回。
缺：生成函数本体，或它参与风控的权重。

**3. 挑战令牌的会话绑定机制只到现象层**

已知：必须同 `rqdata` 同会话，跨会话无效。
缺：绑定的是 cookie、TLS 会话、还是 hCaptcha 内部的 `ekey` 链路。逆向清楚之前，
任何「外部求解后回填」的方案都无法论证可行性。

**4. 通过挑战之后的路径没走通过**

已知：挑战后应当进入 3DS 或直接成功。
缺：真实通过样本。所有纯协议样本都在 confirm 阶段终止，
所以 `verify_challenge` → `poll` → 订阅生效这一段只有代码路径，没有端到端实证。

**5. 免税州地址与非 US 出口的组合未验证**

已知：账单地址默认从美国五个免税州里随机生成，出口按账单国家对齐。
缺：如果业务上需要跑其他国家（JP/GB 等），地址该用什么、税额如何稳定，
没有实测数据。目前的做法是在界面上给出提示。

**6. 金额随税额变化时，前后端展示可能不一致**

已知：勾选 `tax_region` 后后端会同步刷新 `expected_amount`。
缺：前端在提交前后拿到的是两个不同来源的金额（报价 vs init），
金额变化时是否要中断并要求刷新报价，还没定。

**7. Plus 家族的 hCaptcha 与 3DS 分支覆盖不足**

已知：custom 流同样会弹 `intent_confirmation_challenge`（实测 4 张卡里 1 张），
且 `requires_action` 时纯协议无法完成。
缺：挑战命中率样本量太小（4 张），还不足以判断 Plus 与 Team 的挑战率是否相同。

---

## 免责声明与风险提示

**使用本项目前请完整阅读以下内容。使用、部署、修改或分发本项目，即表示你已理解并接受这些风险。**

1. **仅供学习与技术研究**
   本项目是对公开网页行为的协议层研究与工程实践，不构成任何商业服务承诺，也不保证可用于生产环境。

2. **与第三方无关联**
   本项目与 OpenAI、ChatGPT、Stripe 及任何支付、代理服务商均无隶属、授权或合作关系。相关名称、商标归其各自所有者。

3. **依赖非公开接口，可能违反服务条款**
   本项目通过模拟浏览器请求的方式调用 ChatGPT 与 Stripe 的**非公开、未文档化**接口，并使用代理出口与浏览器指纹模拟。这类用法可能违反 OpenAI、Stripe 及相关服务的服务条款或使用政策。上游随时可能修改接口、风控策略或版本，导致功能部分或全部失效，且不会另行通知。

4. **账号风险：可能被限制、风控或封禁**
   使用本项目可能导致用于开通的 ChatGPT 账号、关联的支付方式、代理 IP 乃至相关账户被**风控、限制功能、暂停或永久封禁**，已开通的订阅也可能被取消或回收。这类损失无法恢复，本项目及其作者不提供任何补偿或申诉协助。

5. **资金风险**
   - 扣款成功即视为订单成功，订阅是否真正生效**不作为**交付前提。可能出现「已扣款但订阅未生效」「重复扣款」「被拒付或退款」「金额与预期不符（含税费、汇率、活动价变动）」等情况，请在使用后自行到账号与账单中核对。
   - 促销、优惠活动（`promo`）由上游控制，资格、价格与有效期可能随时变化，滥用促销可能触发风控或被追缴。
   - 3DS 验证、银行风控、卡片限额等均可能导致支付失败或状态不确定。
   - 本项目会真实发起扣款请求。测试请使用自有卡或发卡方提供的测试卡，不要使用他人卡片。

6. **合法授权与使用者责任**
   你必须确保对所使用的账号、Session、银行卡、代理拥有合法授权，并遵守所在地法律法规、上游服务条款与支付网络规则。**严禁**将本项目用于盗刷、使用他人未授权的卡片或账号、欺诈、批量滥用或任何违法用途。「代他人开通并收费」属于你与客户之间的商业关系，是否合法及是否需要资质由你自行判断并承担责任。

7. **数据与安全责任**
   - 本项目不落盘 Session，交付后即销毁。
   - 两种提交方式对卡号的处理不同：**协议提交**模式下卡号会经过本服务内存（不写日志、不入库，仅在单次请求内使用）；**Stripe 输入框**模式下卡号只进入 Stripe 的 iframe，本服务拿不到。
   - 部署环境、代理、日志、反向代理与服务器的安全由使用者自行负责。请勿将真实凭证、卡号、抓包文件（HAR）、代理账密提交到任何仓库或发给他人。

8. **无担保与责任限制**
   本项目按「现状」提供，不附带任何明示或暗示的担保，包括但不限于适销性、特定用途适用性、稳定性与不侵权。在适用法律允许的最大范围内，作者及贡献者不对因使用或无法使用本项目造成的任何直接或间接损失（包括但不限于账号封禁、资金损失、数据丢失、业务中断、第三方索赔）承担责任。

如果你不接受以上任何一条，请不要使用本项目。

---

## 仓库安全

入库内容经过清理，不含服务器地址、真实卡号、代理凭证、打码密钥与后台口令。
敏感值通过 `.env`、`config/`、`data/` 注入，均在 `.gitignore` 内。

商户的 Stripe publishable key 保留在代码里：它是设计上可公开的键，且商户固定。

发现遗漏请开 issue，不要附带任何真实凭证。

---

## 归档

浏览器求解路线已于 2026-10 整体归档到 `attic/browser-route-20261021/`，
包含 hCaptcha 求解器、Camoufox 会话、Chrome 录包与自动付款脚本。

归档原因：挑战令牌必须在持有该 `rqdata` 的会话里签出，跨会话求解结构上无效；
同时浏览器常驻带来 646MB 内存与一套 Xvfb + 证书的运维成本。

探索过程记录在 [docs/exploration-log-browser-route.md](./docs/exploration-log-browser-route.md)，
协议细节在 [docs/hcaptcha-direct-solve.md](./docs/hcaptcha-direct-solve.md)。

---

欢迎加入群聊：https://t.me/+op8u6XEdswg0YTVh
