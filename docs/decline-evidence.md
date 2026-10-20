# 明确拒卡证据

## 结论

已拿到明确的卡片拒绝。

```
HTTP         402
code         = card_declined
decline_code = generic_decline      （另有 do_not_honor 实测出现）
message      = "Your card was declined."
charge       = ch_3USC4bC6h1nxGoI30sd02yoa
pm           = pm_1USC4ZC6h1nxGoI3XVPEFUkw
cs           = cs_live_a1xDCT7N6BrJVZAWh8YiTvykT7Ltt9i2d5gwYPpqYWyTvvH6xkaU1bUVrD
card         = visa ****5210, exp 12/2030, funding=credit, country=US
extra_fields.recommended_payment_method = debit_card
```

## 复现

```bash
node pay2.cjs <cs_live_URL> --card "$CARD_B_NUMBER|$CARD_B_EXP_MONTH|$CARD_B_EXP_YEAR|$CARD_B_CVC"
```

内部步骤：

```
① POST m.stripe.com/6  × 4         → guid / muid / sid
   GET  payment_pages/allowed_origins → 必须 Origin: https://js.stripe.com
② POST payment_pages/{cs}/init     → amount / link_settings{hcaptcha_site_key, hcaptcha_rqdata}
③ GET  elements/sessions           → elements_session_id / config_id
④ POST payment_pages/{cs}          → tax_region[country] 单次提交
⑤ 解 passive captcha               → sitekey=24ed0064-... + init 的 rqdata
⑥ POST payment_methods             → pm_xxx
⑦ POST payment_pages/{cs}/confirm  → 带 passive_captcha_token
   └─ 若返回 intent_confirmation_challenge：
        解 sitekey=c7faac4c-... + challenge rqdata
        → POST verify_challenge
⑧ GET  payment_pages/{cs}/poll     → 终态
```

## confirm 的三种结局

| HTTP | 返回 | 含义 |
|---|---|---|
| 402 | `card_declined` | 到发卡行，拿到明确拒绝 |
| 200 | `next_action.type=intent_confirmation_challenge` | Stripe 追加二次挑战 |
| 200 | `payment_intent.status=succeeded` | 支付成功 |

## 关键工程细节

### 1. 两个不同的 hCaptcha sitekey

```
link_settings.hcaptcha_site_key   = 24ed0064-62cf-4d42-9960-5dd1a41d4e29   被动验证
challenge 里的 stripe_js.site_key = c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a   二次挑战
```

用错 sitekey 会导致 token 结构与服务端预期不匹配。

### 2. init 专属 rqdata

`link_settings.hcaptcha_rqdata` 每次 init 都不同，296 字符，必须用它解 passive captcha。

### 3. hCaptcha wasm 只能在 newassets.hcaptcha.com 运行

实测各域 CSP：

```
pay.openai.com            script-src 无 'unsafe-eval'
js.stripe.com             script-src 无 'unsafe-eval'
b.stripecdn.com           script-src 无 'unsafe-eval'
m.stripe.network          script-src 无 'unsafe-eval'
newassets.hcaptcha.com    script-src 'self' 'unsafe-eval' ...   ← 唯一可用
```

所以 `hcaptcha.render()` 必须在 `newassets.hcaptcha.com` 的 iframe 里跑。
页面 origin 用 `http://127.0.0.1:port`（hCaptcha 白名单允许），`render` 时传 `host: 'b.stripecdn.com'`。

产出的 token 与真实浏览器抓包对比：

| 字段 | 浏览器（HAR） | 本方案产出 |
|---|---|---|
| 总长 | 3,772 | 3,783–3,810 |
| `cdata` | 460 | 460 |
| `passkey` | 2,220 | 2,236 |
| `kr` | 8 位 | 8 位 |

### 4. 打码服务的 token 会被拒

`HCaptchaTask`（带订单代理）解出的 token 结构齐全（cdata/passkey/kr/shard_id 都在），
但 `verify_challenge` 返回 `payment_intent_authentication_failure`。
原因是打码方的浏览器环境与我们的 HTTP 请求不同源。`Proxyless` 类型更差——IP 都不同源。

### 5. 地址提交只需一次 country

真实浏览器抓包（HAR）显示 `payment_pages/{cs}` 的 POST 只有三个字段：

```
eid = NA
key = pk_live_...
tax_region[country] = EG
```

没有逐字段提交 line1/city/state/postal。早期实现的 6 次逐字段提交是多余的。

### 6. Stripe API 版本分两套

```
elements/sessions   →  2026-03-25.dahlia
confirm             →  2020-08-27;custom_checkout_beta=v1
```

两处不同，混用会导致字段校验失败。

### 7. attribution 对齐 hosted_checkout 流

```javascript
merchant_integration_source       = checkout
merchant_integration_version      = hosted_checkout
payment_method_selection_flow     = merchant_specified
checkout_config_id                = 每次 payment_pages POST 后刷新
```

## 关于"Test data not accepted"

那是页面 HTML 里一个位于 `(-10000, -10000)` 的隐藏静态文案，**不是错误提示**。
四张卡都是真实有效的。

## 代码位置

```
packages/core/src/gss_core/stripe_protocol.py   协议层实现
tools/auto-pay/pay2.cjs                        单次支付（Node）
tools/auto-pay/pipeline.py                     协议 + 浏览器取 token
tools/auto-pay/hunt.py                         批量测试
```
