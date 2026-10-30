# Plus 协议模式：2026-10-29 HAR 逐请求复刻

> 证据文件：`1029pluschatgpt.com.har`
> SHA-256 `230330059dc8633c73ed695440ceb7019e7ef03be3fa419e36e7005bdb4e9e38`，614 entries。
> 本文描述的实现默认启用：`GSS_PLUS_ENGINE=har`（不设也是 har）。

## 1. 两种模式，这次改的是「协议」

| 模式 | 前端取值 | 卡号路径 | 后端入口 |
|---|---|---|---|
| 协议 | `pmMode="server"` | 卡号只进服务端内存，直接进 `confirmation_tokens` | `PlusHarProtocol` |
| 输入框 | `pmMode="stripe"` | 卡号只进 Stripe iframe，浏览器换 `pm_` | `PlusProtocol`（未改） |

- `/api/orders/{id}/pay-protocol`：无 `pm_` → `PlusHarProtocol`；有 `pm_` → 旧 `PlusProtocol`。
- `/api/orders/{id}/bind-and-pay-protocol`：`run_checkout_protocol()` 默认转调 `run_har_context()`。
- 回退：`GSS_PLUS_ENGINE=legacy` 恢复旧的 `checkout_v2` 链路（旧集成测试就钉在这个开关上）。

## 2. 请求映射（HAR entry → 代码）

| HAR | 方法/路径 | 代码位置 | 说明 |
|---|---|---|---|
| 1 | `POST /ces/v1/rgstr` | `har_telemetry.ces_register()` | statsig 客户端注册，gzip JSON body |
| 33/34 | `POST /backend-api/sentinel/req` + `/ping` | `_sentinel()` | `chatgpt_checkout` / `checkout_session_approval` 两种 flow |
| 35 | `POST /backend-api/payments/checkout` | `_create_checkout()` | 仅当订单还没有 `oaics_` 会话时 |
| 40 | `GET /backend-api/payments/checkout/{processor}/{id}` | `_read_checkout()` | `?supports_business_seat_offer=true` |
| 65 / 272 | `GET /v1/elements/sessions` | `_elements_session()` | 金额取税费前/后两次 |
| 83–343 | `POST r.stripe.com/b` | `har_telemetry.emit_through()` | 88 个批次、291 条事件 |
| 89 / 98 / 146 | `POST /v1/consumers/sessions/lookup` | `_consumers_lookup()` | 三种 `request_surface` |
| 139/184/186/210 | `POST m.stripe.com/6` | `har_telemetry.register()` | v2:1 / v2:2 / mouse-timings ×2 |
| 215 | `GET /edge-internal/card-metadata` | `_card_metadata()` | `bin_prefix` = 卡号前 6 位 |
| 267 | `POST /backend-api/payments/checkout/taxes` | `_taxes()` | 税额进 `amount_minor` |
| 276 | `POST /backend-api/payments/checkout/update_tax_id` | `_update_tax_id()` | `tax_id_info: null` |
| 294 | `POST /v1/confirmation_tokens` | `_confirmation_token()` | 卡内联 + radar token + guid/muid/sid |
| 330 | `POST /backend-api/payments/checkout/confirm` | `_app_confirm()` | 四字段 JSON |
| 332 | `POST /v1/payment_intents/{pi}/confirm` | `_intent_confirm()` | 同一 ctoken + return_url + `l1` |
| 342 | `GET` 原结账 | `_settle()` | `complete` + `paid` |
| 350 | `POST /backend-api/payments/checkout_session` | `_ack_checkout()` | `success=true` |
| 482 | `GET /backend-api/subscriptions` | `_subscription_active()` | `is_active` + `entitlement` |
| 351/372 | `POST m.stripe.com/6` | `har_telemetry.beat(2)` | 付款后两次心跳 |

`confirmation_tokens` 的字段顺序与取值逐条对齐 HAR：`payment_method_data[...]`、`guid/muid/sid`、
`pasted_fields=number`、`time_on_page`、`setup_future_usage=off_session`、
`set_as_default_payment_method=false`、`client_context[customer]`、`client_attribution_metadata[...]`。

## 3. 设备：跟随本机，按账号持久化

`gss_core/har_device.py::DeviceProfile.random()`：

| 维度 | HAR 里的值（固定） | 现在 |
|---|---|---|
| OS / Chrome | macOS / 156.0.8078.48 | macOS 5 : Windows 3 : Linux 1 加权随机，build 从 136–150 取（与 curl_cffi 的 TLS 指纹一一对应） |
| UA / UA-CH | 一套固定组合 | UA build = `sec-ch-ua-full-version` = `full-version-list`，major 一致 |
| 屏幕 | 2560×1326@2x | 每种 OS 4–5 组真实分辨率/DPR |
| 时区 / 语言 | Asia/Tokyo / en-US | 由账单国家推导，`browser_timezone` 与 `locale` 同步 |
| 插件 / canvas / audio | 一组固定指纹 | 插件串保持 Chrome 桌面形态，canvas 55 位、audio 32 hex 每次新生成 |
| `guid/muid/sid` | 42 字符（UUID+6hex） | 每次新生成，格式不变 |
| `stripe_js_id` | 固定 UUID | 每次新 UUID |
| `elements_session_id` / `sobj` / `element_id` | 固定 | 每次新生成，仍用服务端下发的 session id 覆盖 |

### 设备跟随本机（Linux 就是 Linux）

HAR 是在 macOS 上抓的，但跑协议的机器是 Linux。以前按 macOS 5 : Windows 3 : Linux 1 随机，
Linux 机器报 macOS 的 UA，字体/canvas/WebGL/JS 引擎全是真的 Linux 值，交叉验证直接对不上。

现在：

| 能力 | 行为 |
|---|---|
| 默认 OS | `sys.platform` 自动判定（Linux 服务器就是 `linux`），`GSS_DEVICE_OS=linux|macos|windows` 可强制 |
| 本机真实指纹 | `scripts/fingerprint_probe.js` 用本机 Chrome 采 canvas、audio、WebGL vendor/renderer、核心数、内存、屏幕、时区，输出 JSON |
| 接入 | `GSS_DEVICE_FINGERPRINT=/opt/gss/data/device.json`，`DeviceProfile` 用这份真值：canvas 取 55 位、audio 取 32 hex、cores/screen/timezone 跟随本机 |
| UA / TLS | 仍然成对：UA 的 Chrome build = `curl_cffi` 的 impersonate（chrome136/142/145/146/150），不会出现 `HeadlessChrome` |
| 求解器 | `/solve` 的 `platform` / `meta` / `locale` 全部来自同一个 profile |

### 设备按账号持久化

探针只在本机跑一次。设备本身按 **ChatGPT 账号** 存在 `GSS_DEVICE_STORE`
（默认 `$GSS_DATA_DIR/device_store.json`，0600）：

| 字段 | 跨订单 | 说明 |
|---|---|---|
| `guid/muid/sid`、`frame_host/path/token`、`canvas`、`audio`、`cores`、屏幕、时区 | 不变 | 同一个账号多次结账 = 同一台设备回来 |
| `stripe_js_id`、`elements_session_id`、`elements_session_config_id`、`sobj`、`element_id` | 每次换 | 真实浏览器每次页面加载都会换 |
| `owner` | 固定 | 设备绑定账号；store 里 owner 不匹配时拒绝复用并重建 |

一台机器上的多个账号共用同一套 canvas/audio（同机同内核本该如此），但 `guid` 各自独立——
把 `guid` 也做成全局唯一一台，等于告诉 Stripe「一台设备给几十个不相关账号付款」，比新设备更糟。

TLS/HTTP2 指纹跟着同一份 profile 走：`curl_cffi` 的 `impersonate` 与 UA 的 Chrome major 绑定
（chrome136/142/145/146/150），chatgpt.com、api.stripe.com、m.stripe.com 共用同一套传输指纹。

一致性由 `tests/unit/test_har_device.py` 锁定（UA build、平台 hints、屏幕几何、ID 形状、header 集合）。
设备会写进订单上下文 `har_device`，状态查询复用同一套，不会二次换设备。

### 持久化验收

见上文「设备按账号持久化」。要点：`guid/muid/sid` 与 iframe origin 跟着账号走，
`stripe_js_id`、`elements_session_id` 每次页面加载换新，`owner` 不匹配时拒绝复用。

## 4. 遥测：三段都补齐

1. **`m.stripe.com/6`**（4+2 次）
   `base64(urlquote(JSON))`，`v2:1`（`a=null`）、`v2:2`（15 个信号 `a..o`）、
   `mouse-timings-10-v2`、`mouse-timings-10`；Cookie `m=<guid>`，Origin `https://m.stripe.network`。
2. **`r.stripe.com/b`**（88 次 / 291 条）
   事件目录由 `scripts/har_extract_stripe_events.py` 从 HAR 生成到
   `packages/core/src/gss_core/stripe_events.json`：146 个唯一事件、原始顺序、
   原始 `num_requests` 分批。所有抓包设备值被替换成 `{{token}}`，运行时用新设备填充；
   抓包里的邮箱、`stripe_js_id`、`pk_live_…`、`guid` 不会出现。
3. **ChatGPT 侧**
   `/ces/v1/rgstr`（statsig，gzip body，`statsig-*` 头）+ 每次 `/sentinel/req` 后的 `/sentinel/ping`。

开关：`GSS_HAR_TELEMETRY=full`（默认）/ `off`。

### 验收（生产机实跑）

| 目标 | 数量 | 状态 |
|---|---|---|
| `r.stripe.com/b` | 88 POST | 200 ×88 |
| 事件条数 | 291 | — |
| 唯一事件名 | 146 | — |
| `m.stripe.com/6` | 6（v2:1 ×3、v2:2 ×1、mouse ×2） | 200 ×6 |
| `js.stripe.com`（SDK 资源） | 3 | 200 |
| `m.stripe.network`（指纹 iframe） | 2 | 200 |
| `api.stripe.com` | 7×200 + 1×402 | — |

复验方法：

```bash
ssh gss 'python3 /tmp/capstats.py'     # 统计 /opt/gss/logs/har-capture.jsonl
```

代价：88 次 POST 全走住宅代理，一次 1–3 秒，整条遥测 3–4 分钟。
`GSS_HAR_TELEMETRY_BUDGET`（默认 120 秒）是硬截止，到点丢弃剩余事件，
支付主链不再等尾巴。

## 4b. 代码地图（这次新增的模块）

| 文件 | 职责 | 入口 |
|---|---|---|
| `packages/core/src/gss_core/plus_har_protocol.py` | 逐请求复刻、金额/税费推进、ctoken 与 confirm、结果归一 | `PlusHarProtocol.pay_from_url()` |
| `har_device.py` | 设备画像 + UA/UA-CH/TLS 绑定 + 按账号持久化 | `DeviceProfile.random()`、`device_for_account()` |
| `har_telemetry.py` | `m.stripe.com/6`、`r.stripe.com/b`、CES、资源加载、预算 | `StripeTelemetry.register/emit_through` |
| `stripe_events.json` | 291 条事件模板（采集值已换成占位符） | `scripts/har_extract_stripe_events.py` 生成 |
| `checkout_protocol.py` | 引擎分流：`oaics_` → HAR，`cs_` → 旧链路，`legacy` 回退 | `run_checkout_protocol()` |
| `saved_card_protocol.py` | 输入框模式（已有 `pm_`）的确认流程 | `run_saved_card_protocol()` |
| `scripts/fingerprint_probe.js` | 采本机指纹写 `device.json` | `node scripts/fingerprint_probe.js` |
| `scripts/socks_bridge.py` | HTTP CONNECT → 认证 SOCKS5 | `SOCKS_UPSTREAM=… BRIDGE_PORT=18080` |
| `scripts/plus_har_run.py` | 不经过 API 的单机 CLI | `python scripts/plus_har_run.py` |
| `tools/hcaptcha-solver/server.js` | 求解器：`platform`/`meta`/`locale` 透传，产物目录 `GSS_ARTIFACT_DIR` | `POST /solve` |

## 5. hCaptcha / Radar token（求解器接入）

求解器就是 `tools/hcaptcha-solver`，服务端通过 `GSS_LLM_SOLVER_URL` 调用：

```bash
cd tools/hcaptcha-solver
npm i puppeteer-core            # 首次
ORIGIN_MODE=loopback HEADLESS=true PORT=8199 node server.js

# 另一个终端：API 服务带上求解器地址
GSS_LLM_SOLVER_URL=http://127.0.0.1:8199 ... uvicorn app:app --port 18120
```

链路里的位置：`elements/sessions` 返回 `passive_captcha.site_key` 后立刻求解，
token 同时写进两处：

- `payment_method_data[radar_options][hcaptcha_token]`（ctoken）
- 遥测事件的 `passive_captcha_token`（HAR 里同名字段）

实测：`P1_…` 长度 3,880–3,915，单次 12–30 秒。`ORIGIN_MODE=merchant`
（`https://b.stripecdn.com` + 自签证书 + host-resolver-rules）在本机 macOS +
Chrome 156 上 `api.js` 加载超时，`loopback` 模式正常出 token。

## 5b. hCaptcha / Radar token（原始说明）

`elements/sessions` 返回的 `passive_captcha.site_key`、`rqdata` 会喂给求解器
（`GSS_LLM_SOLVER_URL/solve`），拿到的 `P1_…` 同时写进
`payment_method_data[radar_options][hcaptcha_token]` 和遥测里的 `passive_captcha_token`。
求解器不可用时该字段省略，不阻断流程——与 HAR 的差别只有这一处。

## 6. 验证

```bash
# 设备自洽 + 遥测结构 + 请求顺序
.venv/bin/python -m pytest -q tests/unit/test_har_device.py \
  tests/unit/test_har_telemetry.py tests/unit/test_plus_har_protocol.py

# 从 HAR 重新生成事件目录（改了抓包才需要）
.venv/bin/python scripts/har_extract_stripe_events.py \
  /path/to/1029pluschatgpt.com.har packages/core/src/gss_core/stripe_events.json
```

单测覆盖：请求顺序（sentinel → checkout → elements → lookup → 指纹 → 卡 BIN → 税 →
update_tax_id → ctoken → confirm → intent confirm → 结账回读 → ack）、
ctoken / confirm / intent confirm 三个 body 的字段、拒卡映射、只读不重复扣款、
设备与 Stripe id 跨阶段持久化。

单跑一次（不经过 API）：

```bash
GSS_SESSION_FILE=~/session.txt \
GSS_CHECKOUT_URL='https://chatgpt.com/checkout/openai_llc/oaics_...' \
GSS_CARD='{"number":"4242424242424242","exp_month":"12","exp_year":"2030","cvc":"123"}' \
GSS_BILLING='{"name":"Fixture Holder","email":"a@b.c","line1":"1 Main St","city":"Portland","state":"OR","postal_code":"97211","country":"US"}' \
.venv/bin/python scripts/plus_har_run.py
```

脚本先打印本次随机设备（OS / UA / 屏幕 / 时区 / guid-muid-sid / TLS 目标），再打印
`state / intent_status / decline_code`。

## 7. REA 证据

`rea inspect-web-network-capture`（rea-agents 6.3.0，HAR 适配器 `har-schema@2.0.0`）跑的是
23 条支付记录的脱敏子集，不是 78 MB 原文件——原文件超出 32 MiB 输入预算。

| 项 | 值 |
|---|---|
| Evidence | `ev_80375fdafa90d73994de3fa9075150126ce4c3e64d01e6292f4696a854a3cb35` |
| 子集文件 | `docs/evidence/1029plus-payment-subset.json`（642,958 字节，已脱敏） |
| 子集 sha256 | `c6a8303123a8a97d12323bfcb8efd79d334a1a1bb04b9418c12e280850e9fe22` |
| 原文件 sha256 | `230330059dc8633c73ed695440ceb7019e7ef03be3fa419e36e7005bdb4e9e38` |
| 记录数 | 23（原序号 35…482，1-based） |
| 脱敏 | cookie / authorization / sentinel / oai-did / 账号 id 头置空；JWT、`gAAAA…`、`P1_…`、`cuss_secret`、`pi_…_secret_…`、邮箱、卡号、CVC 替换为 `<redacted>` |

REA 记录的 initiator 把每条请求的发起脚本也钉住了，这决定了「哪些请求必须由浏览器发出、哪些能纯协议复刻」：

| 发起方 | 覆盖的请求 |
|---|---|
| `chatgpt.com/cdn/assets/226913.f2de3ceb87.js` | checkout 建单/读取、taxes、update_tax_id、confirm、checkout_session、subscriptions |
| `js.stripe.com/v3/fingerprinted/js/shared-690527f8d3c420a8fcc6f.js` | elements/sessions、consumers/sessions/lookup、card-metadata、confirmation_tokens、payment_intents confirm、r.stripe.com/b |
| `m.stripe.network/out-4.5.45.js` | 全部 `m.stripe.com/6` 指纹与鼠标时序 |
| `chatgpt.com/sentinel/20260810913b/sdk.js` | `sentinel/req`（函数名 `ge`） |

子集里 6 条 Stripe 响应正文为空（elements/sessions、lookup、card-metadata、confirmation_tokens、intent confirm、m.stripe.com/6），
REA 的 limitations 也写明「不抓取录制 URL、不建立实时归因」；因此实现里这些响应只做防御式解析，缺失字段不推断成功。

## 8. 真实响应体复核（2026-10-30）

抓包文件里 Stripe 响应体是空的，这一节用真实服务器链路补上：本地起 `gss_api`（与部署同一份代码），
用 JP 账号 + live 公钥建一张 Plus 单（3000 JPY），`bind-and-pay-protocol` 走完整协议链路，
`GSS_HAR_CAPTURE=/tmp/gss/capture.jsonl` 把每一次请求与响应落盘。

| 端点 | 状态 | 真实响应要点 |
|---|---|---|
| `GET /v1/elements/sessions` | 200，32.8 KB | `session_id=elements_session_…`、`config_id=<uuid>`、`customer.customer_session.customer=cus_…`、`passive_captcha.site_key=463b917e-…`、`rqdata=null` |
| `POST /v1/consumers/sessions/lookup` | 200 | `exists=false`、`consumer_session=null`、`error_message="No consumer found…"` |
| `GET /edge-internal/card-metadata` | 200 | `{"data":[{"brand":"VISA","country":"GB","funding":"CREDIT",…}]}`（数组，不是单对象） |
| `POST /backend-api/payments/checkout/taxes` | 200 | `total.minorUnitsAmount=3000`，`billing_details.country=JP` |
| `POST /v1/confirmation_tokens` | 400 | `code=card_declined`、`decline_code=live_mode_test_card`、`type=card_error` |
| `POST m.stripe.com/6` | 200 | `{"muid":"…","guid":"…","sid":"…"}`，42 字符，服务端会重新分配 |
| `POST r.stripe.com/b` | 200 ×79 | 空体 |

顺带确认了一个必要字段集：把 `confirmation_tokens` 里的
`payment_user_agent` / `referrer` / `time_on_page` / `pasted_fields` 去掉后，Stripe 直接拒绝：

```json
{"error":{"message":"This publishable key tokenization request is required to use the Stripe SDK."}}
```

复核发现并修掉的五处偏差：

| # | 问题 | 修法 |
|---|---|---|
| 1 | `customer` 是对象，`cus_` 在 `customer.customer_session.customer`，旧解析拿不到 → `client_context[customer]` 缺失 | 解析三级：字符串 → `customer.id` → `customer.customer_session.customer` |
| 2 | 用了随机 `elements_session_config_id`，真实值在 `elements/sessions.config_id` | 解析后写回 `device.elements_session_config_id` |
| 3 | 指纹服务端改发的 `guid/muid/sid` 没回写，ctoken 里还是本地旧值 | `register()/beat()` 末尾回写 `device.guid/muid/sid` |
| 4 | 少发了第 3 个 `consumers/sessions/lookup`（`web_link_authentication_in_payment_element`） | 补在第二个 lookup 位置，带 `customer_id` |
| 5 | ctoken 400 + `card_declined` 被当成 `unknown` | 按 `code` 判拒卡，状态回落 `card_declined` |

### 真卡跑穿（2026-10-30）

本地卡池 `runtime/cards/parsed.json`（95 张，31 张带完整 US 账单）里抽了 4 张，走服务器链路：

| BIN | 类型 | ctoken | ChatGPT confirm | intent confirm | decline_code |
|---|---|---|---|---|---|
| 460223 | Visa Prepaid | 200 | 200 | 402 | `generic_decline` |
| 460223 | Visa Prepaid | 200 | 200 | 402 | `generic_decline` |
| 527515 | Mastercard Debit | 200 | 200 | 402 | `fraudulent` |
| 411774 | Visa Credit | 200 | 200 | 402 | `fraudulent` |

后两张是 Radar 风控拒付（`fraudulent`），前两张是发卡行拒付。四个不同的 BIN 全部停在
`requires_payment_method`，`GET checkout` 仍是 `open/unpaid`，`charge` 字段有值。
两处差别只在缺 `payment_method_data[radar_options][hcaptcha_token]`（本机没起求解器），
HAR 的同一条请求带着 3.6 KB 的 `P1_…`，这是下一步要补的信号。

真实 402 body 带完整 `payment_intent`（`amount=2918`、`currency=jpy`、`status=requires_payment_method`、
`charge=ch_…`），`_settle()` 已经从中取 `charge_id`、`intent_status`，并对金额做一致性拦截。

复核后的实测结果：`state=card_declined / stage=setup / code=payment_declined`，
ctoken 的 `guid/muid/sid` 与 `m.stripe.com/6` 返回完全一致，`config_id` 与最后一次
`elements/sessions` 一致，`client_context[customer]` 为真实 `cus_`。

证据与脱敏样本：

- [stripe-live-20261030.json](./evidence/stripe-live-20261030.json)：本次抓到的真实响应（已脱敏）
- [tests/fixtures/elements_session_live.json](../tests/fixtures/elements_session_live.json)：完整的 live `elements/sessions` body，供单测解析

## 9. 未覆盖

- 3DS：`requires_action` 仍归 `challenge_required`，纯协议不做验证页。
  客户端也带不上 `payment_method_options[card][request_three_d_secure]`——`confirmation_tokens`
  与 `payment_intents/{id}/confirm` 都返回 `parameter_unknown`，这个开关只在商户侧建 PI 时生效。
- `ab.chatgpt.com/v1/initialize`（statsig 初始化，body 是私有编码）与 `ws.chatgpt.com` 不重放。
- HAR 里 Stripe 的响应体（`elements/sessions`、`confirmation_tokens`、intent confirm）为空，
  字段解析按现有 Stripe 公开结构做防御式读取，缺失时不推断成功。
