# 全协议 hCaptcha 直接求解（已跑通）

> 时间：2026-10-20
> 结论：**直接求解可行，无需图像题、无需人工点击，单次 300–370ms**

## 一、结论先行

```
sitekey  24ed0064-62cf-4d42-9960-5dd1a41d4e29（passive）
方式     在 HCaptchaInvisible.html 内调用 window.hcaptcha.execute
结果     P1_ token，3,415–3,474 字符
耗时     307ms / 366ms / 300ms 量级
稳定性   连续 2/2、3/3 成功，未出现图像题
```

工具：`tools/rea-chrome/solve_hcaptcha.cjs`

```bash
node tools/rea-chrome/solve_hcaptcha.cjs              # 解一次
node tools/rea-chrome/solve_hcaptcha.cjs --loop=5     # 连续 5 次
node tools/rea-chrome/solve_hcaptcha.cjs --json       # JSON 输出
```

## 二、token 结构（实测）

```json
{
  "pd": 0,
  "exp": 1792499654,
  "cdata": "(380 字符)",
  "passkey": "(2060 字符)",
  "kr": "23df3457",
  "shard_id": 362406996
}
```

- `cdata` 固定 380–460 字符，随会话小幅变化
- `passkey` 2,060–2,276 字符，随会话变化
- `kr` 每次求解都不同（站点级凭据，会轮换）
- `shard_id` 稳定为 `362406996`

## 三、为什么关键入口是 HCaptchaInvisible.html

完整的 iframe 链（三层 OOPIF，必须 `Target.setAutoAttach` 递归 3 层才能看见）：

```
pay.openai.com/c/pay/cs_live_...
└── js.stripe.com/v3/hcaptcha-invisible-<hash>.html        Stripe 外壳
    └── b.stripecdn.com/stripethirdparty-srv/assets/v35.5/
        HCaptchaInvisible.html?id=<uuid>                    ★ 有完整 hcaptcha API
        └── newassets.hcaptcha.com/captcha/v1/<hash>/static/
            hcaptcha.html#frame=checkbox-invisible         只有 hsw，无 API
            hcaptcha.html#frame=challenge&sitekey=...     只有 hsw，无 API
```

各层能力实测：

| 层 | `window.hcaptcha` | `window.hsw` |
|---|---|---|
| `js.stripe.com/hcaptcha-invisible` | ✗ undefined | ✗ |
| **`b.stripecdn.com/HCaptchaInvisible.html`** | **✓ render/execute/getRespKey/setData/...** | ✗ |
| `newassets.hcaptcha.com#frame=challenge` | ✗ undefined | ✓ function |
| `newassets.hcaptcha.com#frame=checkbox-invisible` | ✗ undefined | ✗ |

**只有 `HCaptchaInvisible.html` 同时具备 API 与作为站点白名单 origin 的身份。**

## 四、为什么不能自己拼 getcaptcha 的 wire

这条弯路已经彻底排除，证据如下。

### 4.1 wire 结构（真品字节级确认）

```
0x92                    array(2)
0xda <len16>            str16，内容 {"type":"hsw","req":"<JWT-A>"}
0xc8 <len16> <type1>    ext16，type 字节随机（实测 0x00 / 0x18）
<密文>                   无尾部字节
```

真品样本（`runtime/rea-capture/samples/`）：

| 文件 | 总长 | ext16 len | 密文 | 明文≈ |
|---|---|---|---|---|
| `gc_24ed0064.bin` | 24,092 | 23,332 | 23,332 | 23,304 |
| `gc_ec637546.bin` | 24,238 | 23,478 | 23,478 | 23,450 |
| 另三份 | 15,859–15,871 | 15,099–15,111 | — | ≈15,071 |

### 4.2 `hsw(1, x)` 的真实语义

```
hsw(1, {})                → 28 字节   （对象被 msgpack 编码，很小）
hsw(1, "hello")           → 33 字节   （5 + 28）
hsw(1, Uint8Array(1000))  → 1028 字节 （1000 + 28）
hsw(1, Uint8Array(15000)) → 15028 字节（15000 + 28）
```

**固定开销恒为 28 字节**（随机 nonce + 认证码 + 长度）。它接受的是**已序列化字节**，不是 JS 对象。

### 4.3 415 的真正原因：会话绑定，不是体积

逐一排除：

| 变量 | 结果 |
|---|---|
| 密文体积 28B / 5KB / 12KB / 15KB / 23KB | 全 415 |
| 用页面自己抓的 JWT-A | 415 |
| 用手工 msgpack 编码的合法 `l` 对象（26,803 字节） | 415 |
| 修正 ext16 结构（含 type 字节） | 415 |
| **把真品 wire 原样回放** | **415** |

最后一条是决定性的：

```
真品 wire（当时确实返回过 200）→ 现在回放 → 415 Unsupported Media Type
```

说明 415 与请求体无关。密文由 wasm 会话密钥加密，该密钥在 `checksiteconfig` 时派生并绑定当前会话；换会话后服务端解不开，直接 415。

### 4.4 `hsw(0, x)` 解密

```
hsw(0, new Uint8Array([1,2,3])) → RuntimeError: unreachable
```

解密需要服务端侧密钥，本地必失败。这与 wasm 逆向的结论一致：AEAD + 随机 nonce，离线伪造密码学上不可行。

## 五、可行路径：让持有 API 的 frame 自己解

```js
// 在 b.stripecdn.com/.../HCaptchaInvisible.html 内执行
const div = document.createElement('div');
document.body.appendChild(div);
const rid = window.hcaptcha.render(div, {
  sitekey: '24ed0064-62cf-4d42-9960-5dd1a41d4e29',
  size: 'invisible',
  host: 'b.stripecdn.com',
});
const res = await window.hcaptcha.execute(rid, { async: true });
const token = res.response;   // P1_...
const ekey  = res.ekey;       // 可能为空
```

实测输出：

```
#1 ✅ token 3446 字符  366ms
#2 ✅ token 3452 字符  307ms
#3 ✅ token 3458 字符  约 300ms
```

## 六、checksiteconfig 的真实行为（修正旧结论）

```json
GET https://api.hcaptcha.com/checksiteconfig
    ?v=<hash>&host=b.stripecdn.com&sitekey=<sk>&sc=1&swa=1

→ {"features":{"custom_theme":true,"enc_get_req":true},
   "c":{"type":"hsw","req":"<JWT-A>"},
   "pass":true}
```

JWT-A 载荷：

```json
{"f":0,"s":4,"t":"w","d":"<292 字符>","l":"/c/f2c292e5...","i":"sha256-...","e":1792499895,"n":"hsw","c":1000}
```

**重要修正**：服务器侧（EG 代理 + httpx 直连）实测 `pass` 同样是 `true`。
说明「出图像题是因为 IP 信誉差」这个判断是错的 —— `checksiteconfig` 从来没拦过。

真正的门槛在 `getcaptcha`：密文必须由**同一会话**的 wasm 密钥生成。

## 六点五、环境对照实验（第二轮）

### 结论：本地能过，服务器不能，且差别不在代码

| 环境 | 页面 | sitekey | 结果 |
|---|---|---|---|
| 本机 Chrome + 本机直连 | 自建同构页 | `c7faac4c` | ✅ **2/2 成功**，369–835ms |
| 本机 Chrome + 本机直连 | 真实 Stripe 页 | `c7faac4c` | ✅ 3/3 成功 |
| 服务器 Chrome + EG 代理 | 自建同构页 | `c7faac4c` | ❌ 0/3，187–197s 超时 |
| 服务器 Chrome + EG 代理 | 自建同构页 | `24ed0064` | ✅ 成功，14.8s |

**同一个页面、同一个 sitekey、同一版本 hCaptcha，只有出口不同。**

### 意外发现：<PROXY_PROVIDER> 对本机做了降级

```
本机   → 代理 CONNECT api.hcaptcha.com:443  →  HTTP 400 Bad Request
服务器 → 代理 CONNECT api.hcaptcha.com:443  →  HTTP 200

本机经代理出口   = <PROXY_IP>  腾讯云 JP（备用池，与 region 参数无关）
服务器经代理出口 = <PROXY_IP>    TE Data（真实 EG）
```

三个 region（US / JP / EG）从本机请求，全部返回同一个腾讯云 JP 出口。
说明代理供应商按来源 IP 做了分级，本机公网 IP 落在降级池里。

### 副产品：确认了 environment 伪装清单

服务器 Chrome 原本有三处自相矛盾，已修（`tools/hcaptcha-solver/server.js`）：

| 项 | 修复前 | 修复后 |
|---|---|---|
| `navigator.platform` | `Linux x86_64`（UA 是 Windows） | `Win32` |
| `navigator.webdriver` | `undefined` | `false` |
| WebGL renderer | `SwiftShader Device (Subzero)` | `ANGLE (NVIDIA, RTX 3060, D3D11)` |
| `userAgentData.brands` | `[]` | 3 个真实品牌 |
| `userAgentData.platform` | `""` | `Windows` |
| `navigator.plugins` | 占位数组 | 5 个带 name/filename 的对象 |
| `navigator.deviceMemory` | 32 | 8 |
| `permissions.query` | 缺失 | 补齐 |

修复后 `24ed0064` 从 24s 提速到 12–14.8s。

### 从真实 bundle 挖到的实现细节

`HCaptchaInvisible.927bac1806c8fde8064f.bundle.js`（10,745 字节）：

```js
// render 必须带 error-callback
f.render(d, {sitekey, size:"invisible", "error-callback": fn})

// execute 前有一步 SEND_FRAUD_SIGNALS：
Object.keys(message.data).forEach(k => {
  const name = `stripe_${sanitize(k)}_${sanitize(message.data[k])}`.substring(0, 10000);
  window[name] = null;      // 值编码在 key 名里
});
```

这些 `stripe_*` 全局变量就是 hCaptcha `identity` 校验的输入。
伪页面已照此实现（`reaInjectSignals`）。

### 新增工具

```
tools/rea-chrome/ip_ab_test.cjs    出口 IP 对照实验台
  · 自建同构页 + --host-resolver-rules 隔离域名
  · 可挂代理，同一 sitekey 多轮求解
  · --keep 保留现场供手动检查

tools/rea-chrome/local_proxy.cjs   本地转发代理
  · 本机无认证端口 → 上游带认证代理
  · 支持 CONNECT 隧道与明文 HTTP
```

## 七、遗留问题

1. **出口 IP 是唯一剩余门槛** —— 本地直连 369ms 拿到 `c7faac4c` 的 token，
   服务器经 <PROXY_PROVIDER> EG 代理 187s 超时。同一页面同一 sitekey，差别只在出口。
2. **代理供应商降级** —— <PROXY_PROVIDER> 对本机公网 IP 返回腾讯云 JP 备用出口，
   且 CONNECT 被 400 拒绝；同一凭证从服务器发出正常。
3. **跨域限制** —— `b.stripecdn.com` 里求得的 token 无法直接 POST 到 `api.stripe.com`，
   需要父页面（`pay.openai.com`）发起 `verify_challenge`，或服务端代为提交。
4. **batch 场景** —— 每次求解必须在该 checkout 会话的 frame 内，无法跨会话复用。

### 下一步两条路

**A. 换代理源**：找能正常 CONNECT 且 IP 未被 hCaptcha 标记的住宅代理。
判据很干脆 —— `c7faac4c` 求解耗时 < 2s 且返回 token 即通过。

**B. 代理侧排查**：向 <PROXY_PROVIDER> 确认 EG 池是否还有真实住宅 IP，
以及为什么 CONNECT 到 `api.hcaptcha.com` 在部分来源被 400。

代码侧已经不需要再改：`ip_ab_test.cjs` 可以直接用来验收任何新代理。

## 八、文件清单

```
tools/rea-chrome/
├── open_debug_chrome.cjs     开可调试 Chrome（9222，独立 profile，host-resolver 映射）
├── cdp.cjs                   极简 CDP 客户端（list/eval/frames/net/har/goto/click/type）
├── recorder.cjs              记录器 v1（只抓主 frame，已弃用）
├── recorder2.cjs             记录器 v2（递归 attach OOPIF，抓 checksiteconfig/getcaptcha）
└── solve_hcaptcha.cjs        ★ 直接求解工具（可 --loop/--json）

runtime/rea-capture/
├── capture2.jsonl            697 条完整记录
├── hits.jsonl                关键端点单独存
└── samples/
    ├── token_0.txt           真品 token 3,847 字符
    ├── token_1.txt           真品 token 3,607 字符
    ├── gc_24ed0064.bin       真品 getcaptcha wire 24,092 字节
    └── gc_ec637546.bin       真品 getcaptcha wire 24,238 字节
```

---

## 九、第四轮：矩阵对照与误判纠正

### 9.1 四个出口全部失败，但本机同一出口成功

| 出口 | 出口 IP / ISP | passive | challenge |
|---|---|---|---|
| 服务器直连 | <SERVER_IP> Psychz Networks | ✅ 5.1s | ❌ 187s review |
| 服务器 + US 代理 | 208.76.160.178 North Central | ✅ 6.9s | ❌ 187s review |
| 服务器 + JP 代理 | （出口查询超时） | ✅ 13.6s | ❌ 187s review |
| 服务器 + EG 代理 | <PROXY_IP> TE Data | ✅ 12s | ❌ 187s review |
| **本机直连** | **<PROXY_IP> 腾讯云 JP** | — | **✅ 3/3，369–835ms** |

**关键：本机公网 IP 就是 <PROXY_IP>，正是上一轮判定为「降级池」的腾讯云 IP。**
所以「IP 被降级」这个结论是错的 —— 同一个 IP 在本机能过、在服务器不能过。

### 9.2 纠正一个持续三轮的误判

之前一直报告 `cells=0`，据此判断「要么复核页、要么无题」。实测把 `cells` 全量打出来后发现：

```
cells: [
  {aria: "Skip Challenge",                      x:348 y:571 w:84 h:39},
  {aria: "About hCaptcha & Accessibility Options", x:28 y:570 w:35 h:35},
  {aria: "Refresh Challenge.",                  x:73 y:570 w:35 h:35},
  {aria: "",                                    x:325 y:273 w:30 h:30},
]
imgs: [], canvases: 0, prompt: ""
```

这 4 个「格子」全是**按钮**（Skip / About / Refresh / 无名 30×30）。
`cells > 0` 并不能说明「有题可答」。真正的判据是：

- `imgs.length > 0` 或 `canvases > 0` → 有题目
- `prompt` 非空 → 有题干
- 否则就是空壳复核页

服务端拿到的是**空壳**：无图、无题干、只有三个按钮。
`canvases` 字段是本轮新增的，用于区分 9 宫格用 canvas 渲染的情况。

### 9.3 环境伪装已达到与本机一致

```json
// 服务器（修复后）
{"ua":"...Windows NT 10.0...Chrome/155.0.0.0...","platform":"Win32","cores":8,"mem":8,
 "gpu":"ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
 "brands":["Google Chrome","Chromium","Not=A?Brand"]}

// 本机
{"ua":"...Macintosh; Intel Mac OS X 10_15_7...","platform":"MacIntel","cores":10,"mem":16,
 "gpu":"ANGLE (Apple, ANGLE Metal Renderer: Apple M5)","brands":["Google Chrome","Chromium","Not(A:Brand"]}
```

两边 `checksiteconfig` 的响应完全一致（`pass:true`，features 相同），
说明判定发生在 `getcaptcha` 之后，且与 checksiteconfig 无关。

### 9.4 剩余差异（未验证）

1. **页面 DOM 富度**：本机求解后 `bodyLen 8216`、`iframes 2`；服务端求解前 `1692`。
2. **真实渲染链路**：本机有真实 GPU（Metal），服务端是 `--use-angle=swiftshader` 软渲染，
   JS 层虽已伪装 GL 字符串，但 `readPixels` / 帧率 / 渲染耗时等旁路信号仍是软渲染特征。
3. **事件时间分布**：本机人类操作产生真实事件间隔；服务端是脚本合成的固定间隔。

### 9.5 下一步（按性价比排序）

**A. 拿掉 swiftshader**：改成 `--use-angle=gl` 或 `--enable-gpu` + Mesa llvmpipe，
让渲染耗时分布接近真机。这是最可能见效的一步，且不需要外部依赖。

**B. 放大 DOM 富度**：在伪页面里注入一个与真实 Stripe 结账页相近的 DOM 骨架
（表单、输入框、按钮、隐藏元素），让 hCaptcha 的 DOM 特征采集有东西可采。

**C. 事件时间分布随机化**：把 humanBeforeExecute 的固定 9–24ms 间隔改成
带抖动和停顿的真实分布（含加速段、减速段、微停顿）。

**D. 换真实浏览器环境**：服务器装 xvfb + Mesa 已是现状；若 A/B/C 都无效，
考虑用真实 GPU 的机器（或 macOS runner）跑 solver。
