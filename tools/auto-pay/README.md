# 自动开通流水线

两种实现：**纯协议**（Python，容器内）与 **协议 + 浏览器取 token**（混合）。

## 一、纯协议

`packages/core/src/gss_core/stripe_protocol.py`，全 HTTP，无浏览器。
走 `hosted_checkout` 流，与真实用户的 ChatGPT 订阅页一致。

```
① m.stripe.com/6 × 4       →  guid / muid / sid
   allowed_origins          →  必须 Origin: https://js.stripe.com
② init                      →  amount / config_id / link_settings
③ elements/sessions         →  elements_session_id / config_id
④ payment_pages/{cs}        →  tax_region[country] 单次提交
⑤ hCaptcha passive          →  sitekey 24ed0064 + rqdata
⑥ payment_methods           →  pm_xxx
⑦ confirm                   →  402 拒卡 / 200 challenge / 成功
   └─ 若 challenge：sitekey c7faac4c → verify_challenge
⑧ poll                      →  终态
```

### 关键参数（与真实浏览器抓包逐字段对齐）

| 环节 | 参数 |
|---|---|
| `init` | 仅 6 个字段：`browser_locale` / `browser_timezone` / `eid` / `key` / `redirect_type` |
| `elements/sessions` | `_stripe_version=2026-03-25.dahlia`、`elements_init_source=checkout`、`hosted_surface=checkout`、`referrer_host=pay.openai.com`、`deferred_intent[amount]` 传实际金额、3 个 `client_betas` |
| `payment_pages` | 只发 `eid` / `key` / `tax_region[country]` |
| `payment_methods` | 24 个字段，无 `allow_redisplay` / `referrer` / `time_on_page` |
| `confirm` | 无 `save_payment_method`；`payment_method_selection_flow=merchant_specified`；`_stripe_version=2020-08-27;custom_checkout_beta=v1` |

时区跟随代理 IP 的地理位置（`ip-api.com` 查询），避免环境矛盾。

## 二、协议 + 浏览器取 token

`pipeline.py`。hCaptcha 的 wasm 只能在 `newassets.hcaptcha.com` 域运行——
所有 stripe 域的 CSP 都缺 `'unsafe-eval'`，所以 passive token 必须由真实 Chrome 产出。

容器内与宿主机协作：

```
容器内 Python                     宿主机 Node + Chrome
  ├─ 建单、init
  ├─ 写 /data/hcap_cfg.json  ───→  监听变化
  │                                ├─ 本地 HTTP 页（origin=http://127.0.0.1:port）
  │                                ├─ hcaptcha.render({host:'b.stripecdn.com'})
  │                                └─ hcaptcha.execute({rqdata})
  ├─ 读 /data/hcap_token.txt ←───  写回 token（≈3,800 字符）
  ├─ payment_methods
  └─ confirm
```

产出的 token 与真实浏览器抓包对比：

| 字段 | 浏览器（HAR） | 本方案产出 |
|---|---|---|
| 总长 | 3,772 | 3,783–3,810 |
| `cdata` | 460 | 460 |
| `passkey` | 2,220 | 2,236 |
| `kr` | 8 位 | 8 位 |

## 三、使用

环境变量：

```bash
export STRIPE_PUBLISHABLE_KEY=pk_live_...
export CARD_A_NUMBER=... CARD_A_EXP_MONTH=... CARD_A_EXP_YEAR=... CARD_A_CVC=...
export CARD_B_NUMBER=... CARD_B_EXP_MONTH=... CARD_B_EXP_YEAR=... CARD_B_CVC=...
export CARD_C_NUMBER=... CARD_C_EXP_MONTH=... CARD_C_EXP_YEAR=... CARD_C_CVC=...
export CARD_D_NUMBER=... CARD_D_EXP_MONTH=... CARD_D_EXP_YEAR=... CARD_D_CVC=...
export GSS_CAPTCHA_API_KEY=...      # 可选，打码服务兜底
export ORDER_PROXY=http://user:pass@host:port
```

批量测试：

```bash
python hunt.py        # ROUNDS=30 TARGET=1，跑到命中为止
python loop_test.py   # ROUNDS=4 TRIES=2，同 session 多次尝试
```

单次支付：

```bash
node pay2.cjs <cs_live_URL> --card "$CARD_A_NUMBER|$CARD_A_EXP_MONTH|$CARD_A_EXP_YEAR|$CARD_A_CVC"
```

## 四、实测结果

`confirm` 的三种结局：

| HTTP | 含义 | 可操作性 |
|---|---|---|
| 402 + `card_declined` | 到发卡行，拿到明确拒绝 | 目标结果 |
| 200 + `next_action` | Stripe 追加 `intent_confirmation_challenge` | 需有效 challenge token |
| 200 + `succeeded` | 支付成功 | — |

`decline_code` 实测出现过 `generic_decline` 与 `do_not_honor` 两种。

详见 `docs/decline-evidence.md`。
