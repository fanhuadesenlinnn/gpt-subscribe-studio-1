# 2026-10-29 Plus HAR：单结账协议差异与证据

## 证据身份与边界

- 原始文件：用户提供的 `1029pluschatgpt.com.har`，只读解析，未复制至仓库或服务器。
- SHA-256：`230330059dc8633c73ed695440ceb7019e7ef03be3fa419e36e7005bdb4e9e38`。
- 大小：81,767,472 字节；614 个 HAR entries。
- 下文序号为原文件 entries 数组的 **1-based** 序号，不按时间重排。
- REA 原文件检查报告 32 MiB 输入预算限制。随后仅保留 12 个请求的脱敏结构子集，删除原始 headers/cookies/body/query 值及标识符；该子集检查成功。
- 子集 Evidence：`ev_336bbe817e35da0cdde8b21b2398938f1f748e29076f51b7deb371d64f72d63c`。该 Evidence 仅证明脱敏子集，完整时序和下列普通参数由本地 JSON 解析另行核对。

## 捕获观察

| 原序号 | 方法及路径 | 观察 |
|---|---|---|
| 35 | POST `/backend-api/payments/checkout` | HTTP 200，原结账 `open/unpaid`；Plus，JP/JPY。 |
| 40 | GET `/backend-api/payments/checkout/{processor}/{checkout_id}` | 原结账上下文；初始总额 1499 JPY。 |
| 65 | GET `/v1/elements/sessions` | 带当前 Customer Session；响应正文缺失。 |
| 267 | POST `/backend-api/payments/checkout/taxes` | US/OR 账单，JPY 保持不变；最终总额 1363 JPY。 |
| 272 | GET `/v1/elements/sessions` | deferred intent 金额更新为 1363 JPY；正文缺失。 |
| 276 | POST `/backend-api/payments/checkout/update_tax_id` | 当前结账，`tax_id_info: null`。 |
| 294 | POST `/v1/confirmation_tokens` | 卡片内联于 `payment_method_data`；`setup_future_usage=off_session`，`set_as_default_payment_method=false`；包含实时 Radar token；响应正文缺失。 |
| 330 | POST `/backend-api/payments/checkout/confirm` | 四字段 JSON，保存选择为布尔 true；返回 `success/payment_intent` 与 client secret。该响应本身不证明付款成功。 |
| 332 | POST `/v1/payment_intents/{id}/confirm` | 使用 **同一 ctoken**；响应正文缺失，不能从 HTTP 200 反推 Intent 状态。 |
| 342 | GET 原结账上下文 | `status=complete`，`payment_status=paid`，总额 1363 JPY。 |
| 350 | POST `/backend-api/payments/checkout_session` | 使用原 oaics ID 与相同 processor；`success=true`。 |
| 482 | GET `/backend-api/subscriptions` | Plus，`is_active=true`，`entitlement.has_active_subscription=true`。 |

对全部 614 条记录的支付路径清点中，没有独立创建 `/v1/payment_methods`，也没有 `/v1/setup_intents` 或先保存卡再重提链的步骤。此结论仅覆盖这份捕获。

## 已修实现差异

1. 新订单协议上下文使用 `protocol_kind=checkout_v2`，复用原 checkout，不进入 SetupIntent preflight，不新建第二个 checkout。
2. 真实账单税费先读取；JPY 零小数；税费后金额变化先返回 `pending/checkout`，由用户显式确认后继续。
3. 修复 `expected_amount_minor` 对 `checkout_state.total.total.minorUnitsAmount` 的读取；保留零值，不使用 subtotal 代替应付总额。
4. confirmation token 在当前请求中创建；只发送普通支付字段、真实卡片和账单、当前 Customer/币种上下文。不发送历史 Radar 值或伪造 Stripe.js 设备/UA/停留时间。
5. ChatGPT confirm 使用四字段 JSON：`checkout_session_id`、`confirm_token`、布尔 `save_payment_method_for_reuse`、`selected_payment_method_type`。
6. Stripe Intent confirm 使用同一 ctoken，并保留当前商户返回 URL；后续读取真实 Intent 与原 checkout 状态。
7. 交付前核对原 checkout 完成、金额币种一致、Plus 实际生效。订阅未生效时保持 pending。
8. acknowledgment 仅在显式 POST 流程推进时发出一次；GET 只读，丢失响应后通过原 checkout 与订阅查询核实，不重放金融确认。
9. 旧版 SetupIntent 订单不自动转换；保留订单线程锁、资料 HMAC、两种付款模式、随机地址与独立账单国家。


## Stripe.js 遥测的 REA 复核

本次以同一原始 HAR 的 SHA-256 核对来源，筛选 111 条支付与遥测记录，删除凭证、正文值及设备标识后交给 REA `inspect-web-network-capture`。原文件超过 REA 32 MiB 输入预算；REA 分析对象是脱敏子集，不是完整原文件。未进行实时请求或修改线上支付。

Evidence：`ev_43e62082b0b13009552b9ad6b6fbbf933e9ed1df602700d32aeefc748b88e547`。

| 项目 | 捕获中可观察事实 | 当前纯协议覆盖 |
|---|---|---|
| `m.stripe.com/6` | 6 次 POST | 已实现：4 次注册 + 付款后 2 次心跳，见 `har_telemetry.register()/beat()` |
| `r.stripe.com/b` | 88 次 POST | 已实现：291 条事件按原顺序/原分批重放，目录由 HAR 生成 |
| `m.stripe.network` | 2 次 GET | 未重放（iframe 资源加载，不产生设备状态） |
| Elements / confirmation token / Intent | 可观察路径、字段与时序 | 业务请求已接入；不等于 SDK 运行时等价 |
| 设备标识、Radar 与验证信号 | 捕获中存在相应字段 | 每次支付随机生成一套自洽设备；Radar token 由求解器提供 |

原 entry 65、272、294、332 的 Stripe 响应正文缺失。遥测请求次数与一次付款完成同时出现，只能证明捕获事实，不能证明其因果关系、最小必需集合、动态生成规则或服务器接受条件。

## 实现落点

- 逐请求复刻：`packages/core/src/gss_core/plus_har_protocol.py`（默认 `GSS_PLUS_ENGINE=har`）
- 设备随机：`packages/core/src/gss_core/har_device.py`
- 指纹与遥测：`packages/core/src/gss_core/har_telemetry.py` + `stripe_events.json`
- 事件目录生成：`scripts/har_extract_stripe_events.py`
- 说明文档：[plus-har-protocol.md](./plus-har-protocol.md)
- REA 复核（23 条支付记录脱敏子集）：`ev_80375fdafa90d73994de3fa9075150126ce4c3e64d01e6292f4696a854a3cb35`

