# hCaptcha 攻坚：当前状态与决定性结论

> 更新时间：2026-10-20（本轮实测）
> 环境：<SERVER_IP> / Ubuntu 24.04 / Chrome 155 + Xvfb / 代理 <PROXY_HOST>

## 一、已经做完并且验证过的

### 1. solver 从"裸接口"升级成"会话池"

旧版每次 `/solve` 起一个新的 Chrome，passive 与 challenge 各一个实例 = 两个出口 IP。
现在：

```bash
POST /session  {proxy, ua}            → {session_id}
POST /solve    {sitekey, rqdata, host, session_id}
DELETE /session/{id}
GET  /sessions
```

passive 与 challenge 走同一个 browser + 同一个 `page`，出口 IP 一致。

### 2. 代理认证方式修正（关键 bug）

Chrome 的 `--proxy-server` **不接受 URL 里的账号密码**：

```
--proxy-server=http://user:pass@host:3010
→ net::ERR_NO_SUPPORTED_PROXIES   （页面全部加载失败）
```

正确写法：

```
--proxy-server=http://host:3010          # 只给协议/主机/端口
page.authenticate({username, password})  # 账号密码走 407 应答
```

旧代码正是用错的方式，所以带代理启动时 `hcaptcha.com/1/api.js` 永远加载失败。

### 3. origin 模式

hCaptcha 的 challenge iframe 会把 `host` 写进 URL：

```
loopback:  http://127.0.0.1:<port>  →  host=127.0.0.1
merchant:  https://b.stripecdn.com  →  host=b.stripecdn.com   ← 与真实浏览器一致
```

merchant 模式实现：

```
ORIGIN_MODE=merchant
MERCHANT_HOST=b.stripecdn.com
MERCHANT_PORT=8443
CERT_DIR=/opt/gss/tools/hcaptcha-solver/certs   # openssl 自签 CN=b.stripecdn.com

启动参数：
  --ignore-certificate-errors
  --host-resolver-rules=MAP b.stripecdn.com 127.0.0.1:8443
  --proxy-bypass-list=<-loopback>;b.stripecdn.com
```

merchant 模式的 origin 服务是**进程级单例**，不能随会话关闭，
否则后一个会话拿到 `net::ERR_CONNECTION_REFUSED`。

### 4. 行为数据预热

hsw 把 `mousemove`/`pointermove` 写进 manifest。零鼠标事件的会话等于零行为数据。
现在开会话后先跑 4 段贝塞尔曲线轨迹，每次 `execute` 前再加 20 余步抖动。

## 二、决定性结论：c7faac4c 在当前环境必出图像题

### 实测矩阵

| 变量 | 结果 |
|---|---|
| origin = 127.0.0.1，无鼠标 | `Select the TWO shortest towers` |
| origin = b.stripecdn.com，无鼠标 | `Select the thing that moves...` |
| origin = b.stripecdn.com，有鼠标轨迹 | 同上（题目文字不变） |
| 同一会话先解 passive（成功 3602 字符）再解 challenge | 仍出图像题 |
| 有 challenge rqdata（264 字符，真实） | 仍出图像题 |
| passive sitekey 24ed0064 | **不需要图像题，直接返回 token** |

### 判据

1. `24ed0064`（passive）→ 秒出 token，3,600–3,900 字符，从不出题。
2. `c7faac4c`（challenge）→ 一律出图像题，且带 `Please try again. ⚠️`
   （hCaptcha 对本次会话的复审判定，不是题目本身）。
3. 题目类型是**图像识别类**（towers / 移动物体匹配），不是 canary/checkbox。
4. 打码服务 YesCaptcha 余额为 0：`ERROR_ZERO_BALANCE`，两条兜底路径全废。

结论：出题的决定因素不是 origin、不是鼠标、不是 rqdata 新旧，
而是 **c7faac4c 的难度策略 + 当前出口 IP 的信誉**。
数据中心代理（<PROXY_PROVIDER>）在 hCaptcha 眼里属于高风险来源。

### 关键补充：低信誉会话根本不出题

最后一次实测（真实 challenge rqdata，merchant origin，人类轨迹，同会话 passive 已过）：

```
challenge solve → execute_timeout
  prompt: (空)
  header: "Please try again. ⚠️ / Verify"
  imgs:   0
  cells:  0
```

意思是 hCaptcha 连题目都没发，直接把会话判为需要复核。
这种情况下**任何作答都不可能通过**（没有格子可点）。

三种状态要分清：

| 状态 | challenge iframe 内容 | 能否通过 |
|---|---|---|
| 正常 | `Select ...` + 9 宫格图 | 需要答对 |
| 降级 | `Select the TWO shortest towers` 等图像题 | 需要答对 |
| 复核 | `Please try again. ⚠️ / Verify`，无图无格 | 不可能 |

当前出口（<PROXY_PROVIDER> 数据中心代理）落在第三种。
这是 IP 信誉问题，不是代码问题。

## 三、Stripe 侧 verify_challenge 的真实参数

从 `https://js.stripe.com/v3/` 和 `hcaptcha-inner-*.js` 反查得到：

```js
// js.stripe.com/v3/fingerprinted/js/hcaptcha-inner-7e59a27c06e517d19a705cf5491d0c5a.js
e.innerAction("VERIFY_CAPTCHA_CHALLENGE", {
  clientSecret,
  response: e.response,       // hCaptcha token
  ekey: e.ekey,               // hCaptcha ekey（可能为空）
  verifyUrl: h,               // /v1/payment_intents/{pi}/verify_challenge
  captchaVendorName: "hcaptcha",
  expand: _
})
```

服务端表单对应：

```
client_secret
challenge_response_token     ← response
captcha_vendor_name=hcaptcha
captcha_response_key         ← ekey（有才带）
key, _stripe_version
Origin: https://js.stripe.com
```

早期代码缺 `captcha_response_key`。solver 现在会把 `ekey` 一起返回。

## 四、服务器上的服务

| 单元 | 端口 | 说明 |
|---|---|---|
| `xvfb.service` | :99 | 1920x1080x24 + GLX + render |
| `gss-solver.service` | 127.0.0.1:8199 + 127.0.0.1:8443 | ORIGIN_MODE=merchant |
| `gss-api.service` | 127.0.0.1:18120 | uvicorn，非容器 |
| `gss-window.service` | — | 轮转调度器（3 分钟一轮） |
| `caddy` | :80 / :443 | <PAY_HOST> → 18120 |

## 五、下一轮要试的

### A. 换住宅代理

出题与 IP 信誉强相关。同一套代码，换成住宅 IP 出口再测
`c7faac4c` 是否降级为 checkbox。判据很干脆：

```
只有 token 长度 > 3000 且没有 image 题 → 才算过
```

### B. 图像题自动作答

题目是图像选择（"选最短的两座塔"、"选与示例同区域的移动物体"）。
可行做法：

1. 从 challenge iframe 抓 9 宫格图（`imgs3.hcaptcha.com` 的 PNG）
2. 用视觉模型判类型 → 返回 1–3 个格子索引
3. 点选后提交

solver 已经把 challenge iframe 的 DOM 和截图能力打通，接入点：

```js
const fr = page.frames().find(f => /challenge/.test(f.url()));
const cells = await fr.evaluate(() => [...document.querySelectorAll('div[role=button]')]
  .map((d, i) => ({ i, label: d.getAttribute('aria-label') })));
await fr.click(`div[role=button]:nth-child(${idx + 1})`);
```

注意：`Please try again` 说明当前会话已被判可疑，这种情况下答对也可能被拒，
必须先解决 IP 信誉。

### C. 打码服务充值

`ERROR_ZERO_BALANCE` 直接堵死两条兜底。充上后路径 B（`HCaptchaTask` + 订单代理）
才有意义——它的出口与支付同源，是唯一可能通过 verify_challenge 的打码方式。

## 六、代码落点

```
tools/hcaptcha-solver/server.js              会话池 + merchant origin + 人类轨迹
packages/core/src/gss_core/stripe_protocol.py
  _solver_open_session / _solver_close_session
  _solve_via_http                           带 session_id、返回 ekey
  pay()                                     先开会话，challenge 复用会话，最多 2 轮
  verify_challenge                          补 captcha_response_key
/etc/systemd/system/gss-solver.service       ORIGIN_MODE=merchant
```

## 六点五、本轮落地的代码变更

```
tools/hcaptcha-solver/server.js
  + 会话池       POST /session, DELETE /session/{id}, GET /sessions
  + splitProxy() --proxy-server 只给 host:port，账号密码交给 page.authenticate
  + ORIGIN_MODE  loopback / merchant（https://b.stripecdn.com + host-resolver-rules）
  + 共享 origin  merchant 模式的 origin 是进程单例，会话关闭不能关它
  + humanWarmup  贝塞尔轨迹预热 + 每次 execute 前抖动
  + captureChallenge()   抓 prompt / header / imgs / cells
  + answerChallenge()    按 iframe 绝对坐标点九宫格
  + submitChallenge()    点 Verify/Submit
  + readToken()          提交后从 render callback 取 token
  + /challenge           作答 + 提交 + 等 token 的完整接口

packages/core/src/gss_core/stripe_protocol.py
  + _solver_open_session / _solver_close_session
  + _solve_via_http 带 session_id，返回 ekey
  + pay()  先开会话；challenge 复用会话；最多 2 轮续挑战
  + verify_challenge 补 captcha_response_key

packages/core/src/gss_core/captcha.py
  + verify_challenge(..., ekey="")  → captcha_response_key

tests/unit/test_captcha.py
  + test_verify_includes_ekey_when_present
  + test_verify_omits_ekey_when_empty
    全量 224 项通过

服务器
  /etc/systemd/system/gss-solver.service   ORIGIN_MODE=merchant 等 6 个环境变量
  /opt/gss/scripts/probe_once.py           单轮完整探针（含 challenge + verify）
  /opt/gss/scripts/window_scheduler.py     改为解析 RESULT 行，统计 challenge_kind
```

## 七、一句话状态

```
passive  ✅ 稳定出 token（3,600–3,900 字符）
confirm  ✅ 稳定拿到 402 拒卡 或 intent_confirmation_challenge
challenge ❌ 卡在图像题 + 低信誉 IP
verify   ⏸ 参数已对齐，等一个能过图像题的 token
```
