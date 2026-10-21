# Plus 走协议模式的实施计划

> **状态（2026-10-21）：已实现并验证。** 阶段 1–3 完成，
> Plus 家族走 `/pay-protocol` 的 custom 分支，4 张卡实测 4/4 拿到拒卡。
> 实施中发现的三个关键点见文末「实施记录」。

目标：让 Plus / Go / Pro 家族也能像 Team 一样，不发浏览器、不调打码，纯服务端跑完支付。

---

## 1. 现在为什么不通

| 环节 | 现状 |
|---|---|
| 结账类型 | Plus 是 `custom checkout`，会话 id 是 `oaics_`；Team 是 `hosted`，`cs_live_` |
| `/pay-protocol` | `StripeProtocol.pay_from_url()` 用正则取 `cs_live_`，取不到直接 `ValueError` → 502 |
| `/pay` 编排管线 | 需要 `payment_method_token`（`ctoken_` 或 `pm_`），前端不产 |
| 卡在哪 | **没有代码在服务端为 Plus 造 `pm_`** |

所以不是链路缺失，是入口缺失。

---

## 2. 可行性依据（这些代码已经存在）

| 能力 | 位置 | 说明 |
|---|---|---|
| `pm_` → `ctoken_` | `chatgpt.py: create_confirmation_token()` | `POST /v1/confirmation_tokens`，body 带 `key` / `payment_method` / `setup_future_usage=off_session` |
| `pm_` 可直传 | `adapter.py: _confirmation_token()` | 收到 `pm_` 就调上面的方法换成 `ctoken_`，收到 `ctoken_` 直接用 |
| Plus 的 publishable key | `chatgpt.py: _publishable_key()` | `GET /backend-api/payments/stripe_client_bootstrap`，建单时已写入订单 |
| ChatGPT 侧确认 | `chatgpt.py: confirm_checkout()` | `POST /backend-api/payments/checkout/confirm`，body 带 `checkout_session_id` + `confirmation_token` |
| 取 PaymentIntent | `chatgpt.py: finalize_stripe_custom_checkout()` | 最多 12 次、0.8s 间隔轮询 |
| Stripe 侧确认 | `chatgpt.py: confirm_stripe_intent()` | `pi_` 走 `payment_intents`，`seti_` 走 `setup_intents` |
| Sentinel 令牌 | `chatgpt.py: mint_sentinel()` | 挂 `openai-sentinel-token` 与 `openai-sentinel-so-token` |
| 税费对齐 | `chatgpt.py: _sync_billing_tax()` | `oaics_` 下是 **strict** 模式，拿不到税额就中止 |

结论：整条链都能服务端完成，只缺「造 `pm_`」这一步和路由分流。

---

## 3. 缺口清单

| 编号 | 缺口 | 工作量 |
|---|---|---|
| A | 服务端为 custom 流造 `pm_` | 半天 |
| B | 把 A 接上 ctoken → confirm → intent 的完整方法 | 1–2 天 |
| C | `/pay-protocol` 按 checkout id 前缀分流 | 半天 |
| D | 前端放开非 Team 档位 | 半天 |
| E | 端到端验证与文档更新 | 1 天 |

---

## 4. 分阶段实施

### 阶段 1：验证 custom 流的 `pm_` 能不能造出来

**目的**：先花最小代价确认 Stripe 接受我们的字段组合，再动手写链路。

```python
# scripts/plus_probe.py
# 1) 建一张 Plus 订单，拿到 oaics_ 与 publishable_key
# 2) 用这个 pk 向 /v1/payment_methods 提交卡号
# 3) 打印 Stripe 响应码与 pm_ id
```

要点：
- custom 流的 `client_attribution_metadata` 与 hosted 不同，先照 hosted 那套试，
  再按报错逐个调（`merchant_integration_source` / `payment_method_selection_flow`）
- 这一步**不产生扣款**，只创建 PaymentMethod

**验收**：返回 `200` 且拿到 `pm_…`。

### 阶段 2：实现 `pay_custom()`

新建 `packages/core/src/gss_core/plus_protocol.py`，复用现有 `ChatGPTClient`：

```
① mint_sentinel(flow="chatgpt_checkout", page_url=checkout_url)
② POST /v1/payment_methods           → pm_          （阶段 1 验证过的字段）
③ POST /v1/confirmation_tokens       → ctoken_      （已有方法）
④ POST /backend-api/payments/checkout/confirm        （带 Sentinel + ctoken_）
⑤ finalize_stripe_custom_checkout    → PaymentIntent（已有方法）
⑥ confirm_stripe_intent              → 终态        （已有方法）
⑦ 归一成 PaymentOutcome
```

对外签名与 `StripeProtocol.pay_from_url()` 保持一致，方便上层共用结果类型：

```python
def pay_plus(self, checkout_url: str, publishable_key: str) -> PaymentOutcome
```

**注意**：`confirm_checkout` 内部已经会调 `_sync_billing_tax`，且对 `oaics_` 是 strict ——
税额同步失败会直接抛 `DomainError`，这条要在计划里留出排查时间。

### 阶段 3：API 分流

`services/api/src/gss_api/routers/orders.py` 的 `/pay-protocol`：

```python
cs = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", checkout_url)
oaics = re.search(r"oaics_[A-Za-z0-9]+", checkout_url)

if cs:      # Team hosted
    sp = StripeProtocol(...); return sp.pay_from_url(checkout_url)
elif oaics: # Plus custom
    pp = PlusProtocol(...);  return pp.pay_plus(checkout_url, pk)
else:
    raise 400 checkout_upstream_error("无法识别的结账会话")
```

请求体加可选字段：

```python
publishable_key: Optional[str] = Field(default=None, max_length=120)
```

不传时从订单的 `view.publishable_key` 取。

### 阶段 4：前端放开

- `ProtocolPayPanel` 不再假设 Team：金额/币种按 `order.currency` 显示
- `OrderNew` 里协议面板对全部档位可用
- 结果文案沿用现有四态（`paid` / `card_declined` / `challenge_required` / 未知）

### 阶段 5：验证

| 验证项 | 方法 |
|---|---|
| 单卡能到 confirm | 用一张测试卡跑一单 Plus，看是否拿到 402 或成功 |
| 税额同步 | 观察日志里 `_sync_billing_tax` 是否成功 |
| Sentinel | 日志确认 `mint_sentinel` 拿到 token |
| 3DS 分支 | 若返回 `requires_action`，确认错误信息可读 |
| 文档 | 更新 README 的「支持的档位」与「实测数据」 |

---

## 5. 风险与未定项

| 风险 | 影响 | 应对 |
|---|---|---|
| custom 流造 `pm_` 的字段组合未验证 | 阶段 1 可能失败 | 先探测，按 Stripe 报错调字段 |
| 税同步是 strict | 拿不到税额直接中止 | 记录 `taxes` 接口的响应，必要时放宽为 best-effort |
| hCaptcha 在 custom 流是否弹 | 未知 | 阶段 5 实测；若弹且无法绕过，Plus 与 Team 结论一致 |
| 3DS | `confirm_stripe_intent` 可能返回 `requires_action` | 纯协议无法完成，与 Team 同样处理：标记 `challenge_required` |
| Sentinel flow 名 | `chatgpt_checkout` 是否适用于支付阶段 | 阶段 1 一并观察 confirm 的 401/403 |
| `checkout_approval_internal_error` | ChatGPT 侧内部错误 | 已有 `_APPROVAL_RETRIES` 重试，保留 |

---

## 6. 顺序建议

```
阶段 1（探测）  →  阶段 2（实现）  →  阶段 3（分流）  →  阶段 4（前端）  →  阶段 5（验证）
     ↑
   先做这个：花半天确认可行性，避免在错误前提下写两天代码
```

阶段 1 是**决策点**：如果 Stripe 不接受服务端造的 `pm_`，整条路要改走
「服务端用 Stripe.js 等价签名」或「保留浏览器产 ctoken」，届时再评估。

---

## 7. 实施记录（2026-10-21）

### 结果

4 张卡全部拿到拒卡：

```
[A] <CARD>  card_declined  generic_decline
[B] <CARD>  card_declined  generic_decline
[C] <CARD>  card_declined  generic_decline
[D] <CARD>  card_declined  ChatGPT 侧确认 failed
```

单次约 14 秒。

### 计划外的三个发现

**1. Plus 的会话 id 有两种形态**

计划里假设 Plus 一定是 `oaics_`。实测同一账号连续建单，
返回过 `oaics_…` 也返回过 `cs_live_…`（都在 `chatgpt.com/checkout/…` 下）。
所以分流不能看 URL 前缀，要看订单的 `checkout_kind`：

```python
is_custom_checkout = view.checkout_kind == "custom"
```

`cs_` 形态还需要多走一次 `finalize_stripe_custom_checkout` 才拿到 PaymentIntent。

**2. ctoken 的 `setup_future_usage` 与 confirm 的保存选择必须配对**

上游报 `Card save choice does not match confirmation token`。
代码按三组依次尝试：

```
(None,        None)   ← ChatGPT 侧接受这种
(off_session, true)
(on_session,  true)
```

**3. pm 必须声明 `allow_redisplay=always`**

这一条最隐蔽：即使保存选择配对，订阅场景下仍会被拒。
加上之后 B 卡从「保存选择不匹配」变成正常拒卡。

**4. intent 确认要用单独的 ctoken**

ChatGPT 侧接受不带 `setup_future_usage` 的 token，但 Stripe 的
`payment_intents/confirm` 要求带 `off_session`，否则报
`The setup_future_usage value on the ConfirmationToken you provided was null`。
所以第 ⑤ 步单独造一个 ctoken。

### 未覆盖

- 3DS：`requires_action` 时纯协议无法完成，标记 `challenge_required`
- hCaptcha：custom 流同样会弹（4 张里 1 张），样本量不足以对比两族命中率
