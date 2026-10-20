# hCaptcha 攻坚过程记录

## 起点目标
全协议化、全自动化：不碰浏览器，纯 HTTP 把 Stripe 的 `intent_confirmation_challenge` 过掉，
拿到可用的 `P1_` token 喂给 `verify_challenge`。

## 阶段一：纯协议逆向（有价值，已留存）

### 已确认的事实
- 挑战类型：`intent_confirmation_challenge` = hCaptcha Enterprise
- token 结构：`P1_` 前缀 JWT，payload keys `['cdata','exp','kr','passkey','pd','shard_id']`
- 挑战获取：
  `POST api.hcaptcha.com/checksiteconfig?host=b.stripecdn.com&sitekey=c7faac4c-...`
  → `{features:{enc_get_req:true}, c:{type:'hsw', req:'<JWT>'}}`
- `req` payload 字段：`{f,s,t,d,l,i,e,n,c}`
  - `d` = 加密挑战（base64）
  - `l` = wasm 路径 `/c/<hash>`
  - `i` = SRI `sha256-...`
  - `n` = `hsw`，`e` = 过期时间戳，`c` = 1000

### proof 求解（已跑通）
- 正确签名是 `hsw(reqString, options)`，**不是** `hsw(token, key)`
  ```js
  options = { href, ardata: null, vm_data, uj_data, errors, messages }
  ```
- 依据：运行时 `ts()` → `n(i.req, o)`
- 产出：11,523 字节 proof（base64 15,308 字符）

### 加解密原语
- `os(data)` = `hsw(1, msgpack.encode(data))`  加密
- `ns(buf)`  = `hsw(0, buf)` → `msgpack.decode` 解密
- `rs(t)`    = `msgpack.encode(t)`
- msgpack 已独立提取：49,728 字节 / 34 个 browserify 模块

### getcaptcha 请求线格式（字节级确认）
```
92              array(2)
da 02 f0        str32(752)
{"type":"hsw","req":"<JWT>"}
c8 5f16         uint16(24342)
<24342 字节>    os(除 c 外的整个 l 对象)
```
构造逻辑（运行时源码）：
```js
var c = JSON.parse(JSON.stringify(l));
delete c.c;
a = rs([l.c, os(c)]);   // l.c 是 spec 的 JSON 字符串
```

### `l` 对象完整字段
```js
{ v, sitekey, host, hl, motionData, action, extraData, pd, pdc,
  pem, ws, n, e, c, auth_token, pst }
```
`pem` = performance timing（`csc`/`csch`/`cscrt`/`cscft`/`gc`/`gch`/`gcrt`/`gcft`）

### 排除项（重要，省得再试）
| 变量 | 结论 |
|---|---|
| Content-Type | `octet-stream`/`json`/`x-msgpack` 全 415；`form-urlencoded` 才进业务层 |
| body 大小 | pad 到 24,989 字节仍 415，与体积无关 |
| Cloudflare cookie | 带上 `__cf_bm` 仍 415 |
| sitekey / UA / sec-fetch-* | 与结果无关 |

### 死点
- `os()` 加密出的块服务端解不开 → 415
- 根因：`analytics-vm._manifest` 恒为空（VM 从未启动）
- VM 是闭包私有（`var Pi = new Wi()` 不导出），`Mi(2,t)` 初始化失败被 `try/catch` 吞掉
- 结论：**无真实交互 = 无有效 proof**，纯协议构造在此撞墙

## 阶段二：Electron 真机（本机，已跑通但偏航）

- 本机 Apple M5，WebGL renderer = `ANGLE Metal Renderer: Apple M5`
- 关键修正：`host`/`sitekey` 由 **URL hash** 传入，不是 postMessage
  ```
  hcaptcha.html#frame=challenge&id=..&host=b.stripecdn.com&sitekey=..&hl=en-US&endpoint=..&assethost=..&imghost=..
  ```
- 结果：`checksiteconfig 200` / `hsw.js 200` / `getcaptcha 200` / `challenge.js 200`
- 但落到 `image_label_area_select` 图像题
- 根因：伪造 `create-challenge` 时传了 `manifest: {}`，等于自曝"零行为数据"

## 阶段三：服务器（跑通渲染，但方向偏了）

### 环境事实
- VPS: Xeon E3-1230 V2 / 32G / Ubuntu 24.04 / **GPU = Matrox mgag200（无 3D）**
- 安装 Xvfb + Mesa：`llvmpipe (LLVM 20.1.2, 256 bits) OpenGL 4.5`，133 个 GL 扩展
- Electron 24 + Node 20 已装，`--no-sandbox` 必需（root）
- 三档渲染对比：
  - `default`       → `ANGLE (Mesa, llvmpipe, OpenGL 4.5)` ← 指纹最自然
  - `llvmpipe`      → WebGL 不可用
  - `swiftshader`   → `SwiftShader Device (Subzero)` ← 明显软件渲染

### 真实结账页（成功）
- SwiftShader + `--disable-gpu-compositing` + `disableHardwareAcceleration()` 稳定不崩
- 代理认证正确写法：`app.on('login', (e, wc, details, authInfo, cb))`
- 页面完整渲染：$50 / Card+PayPal / 地址 / Subscribe
- 网络：`payment_pages/init 200`、`elements/sessions 200`、`hcaptcha api.js 200`

### 表单填写发现
- 字段名：`cardNumber` / `cardExpiry` / `cardCvc` / `billingName` /
  `billingAddressLine1` / **`billingLocality`** / **`billingPostalCode`**
- 点击 Card 单选框后卡表单展开
- 坐标法不可靠（页面滚动后失效），需每次重新取 rect 或 `focus()` + 键盘

## 为什么会偏
从"纯协议"滑到"驱动浏览器填表"，等于把自动化建立在 UI 操作上，
脆弱、慢、且依赖 GPU 环境。这不是原定目标。

## 回到主线
纯协议路线的真正缺口只有一个：**`l` 对象里的行为数据（motionData/pd/pdc/pem）**。
下一步应聚焦：
1. 让 `hsw` 在 Node 里以正确状态初始化（复现 `Mi(2,t)` 与 `Yr(spec)` 时序）
2. 用合成但结构合规的 `motionData` 填充 `l`
3. 直接构造 getcaptcha body，绕开一切浏览器

## 最终结论（后续验证）

方向判断修正：**浏览器不是弯路，是必经**。

关键实测：

```
所有 stripe 域的 CSP 都缺 'unsafe-eval'
  pay.openai.com / js.stripe.com / b.stripecdn.com / m.stripe.network
只有 newassets.hcaptcha.com 有 'unsafe-eval'
```

hCaptcha 的 wasm 只能在 `newassets.hcaptcha.com` 的 iframe 内实例化。
在 stripe 域里直接调 `WebAssembly.Module()` 会抛 `EvalError`。

可行的取 token 方式：

```
页面 origin  = http://127.0.0.1:<port>      （hCaptcha 白名单允许）
api.js       = https://hcaptcha.com/1/api.js （它自己创建 newassets iframe）
render host  = b.stripecdn.com              （sitekey 的白名单域）
rqdata       = link_settings.hcaptcha_rqdata （每次 init 不同，296 字符）
```

产出的 token 与真实浏览器抓包一致：

| 字段 | 浏览器 HAR | 本方案 |
|---|---|---|
| 总长 | 3,772 | 3,783–3,810 |
| cdata | 460 | 460 |
| passkey | 2,220 | 2,236 |

约 55% 概率 confirm 直接返回 402 `card_declined`，其余触发二次挑战。

所以现在的架构是「协议为主 + 浏览器只负责产 token」，
既不是纯协议硬啃 wasm，也不是浏览器驱动填表。
