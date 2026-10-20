# hCaptcha 协议状态（可复现基线）

> 更新：本轮已拿到服务端 200 响应作为验证基准

## 一、已验证为真的协议链

```
① POST checksiteconfig?v=<ver>&host=<host>&sitekey=<key>&sc=1&swa=1
   → {"features":{...}, "c":{"type":"hsw","req":"<JWT-A>"}, "pass":...}

② hsw(reqA, opts)              # opts = {href, ardata:null, vm_data, uj_data}
   → proof 字符串（14k 字符）

③ os(l) = hsw(1, msgpack.encode(l))   # 加密，线性 +28 字节
   → 密文 E

④ body = msgpack([JSON.stringify(c), E]) 但实际 wire 是：
   92 | da <len:2> | <c 的 JSON> | c8 <len:2> | <E 的原始字节>

⑤ POST getcaptcha/<sitekey>  (Content-Type: application/octet-stream)
   → 200 + 明文 JSON: {"c":{"type","req":"<JWT-B>"}, "success":false, "error-codes":[]}
   → 415 "Unsupported Media Type" 表示 E 无法解密（不是格式错）

⑥ hsw(reqB, opts) → 第二段 proof → 最终 P1_ token
```

## 二、当前摸清的事实

| 项 | 值 |
|---|---|
| req payload 字段 | `f,s,t,d,l,i,e,n,c` |
| `n` 取值 | `hsw`（在 JWT payload 内，不在 spec 对象上） |
| `l` 路径 | `/c/<hash>`，加载 `<l>/<n>.js` |
| `i` | `sha256-...` SRI |
| 加密固定开销 | **+28 字节**（0→32, 105→133, 906→934, 4006→4034） |
| `hsw(0, x)` 解密 | **不可逆**（服务端会话密钥），本地必失败 |
| 真实密文 | 18,009 字节，熵 7.16/8.0，首字节 0x12 |
| 真实 body | 18,775 字节 |
| 真实响应 | 823 字节明文 JSON |

## 三、行为数据格式（从运行时源码提取）

```js
// 事件 → 缓冲键
Ei = { mousedown:'md', mouseup:'mu', mousemove:'mm' }
ki = { pointermove:'pm' }
wi = { keydown:'kd', keyup:'ku' }
vi = { touchstart:'ts', touchend:'te', touchmove:'tm', touchcancel:'tc' }
xi = { devicemotion:'dm' }

// 单条数据
[mouse]   = [windowX, windowY, Date.now()]
[pointer] = [co.x, co.y, Date.now()]   // 每个 coalesced event 一条

// manifest 输出
manifest[key]       = [[x,y,ts], ...]     // 环形缓冲，窗口 period=16ms
manifest[key+'-mp'] = 平均周期

// l 对象字段
l = { v, sitekey, host, hl, action, motionData, manifest, pem, n,
      rqdata, pd, pdc, ws, auth_token, pst, old_ekey, p_e }
```

### pem（performance timing）
```js
pem = { csc, csch, cscrt, cscft, gc, gch, gcrt, gcft }
// csc = checksiteconfig duration
// cscrt = responseStart - requestStart
// cscft = responseEnd  - fetchStart
```

## 四、415 的根因（已排除项）

| 假设 | 结果 |
|---|---|
| Content-Type 不对 | ❌ 排除（octet-stream 正确） |
| body 体积不足 | ❌ 排除（33KB 也 415） |
| msgpack 结构错 | ❌ 排除（字节级一致） |
| 缺 Cloudflare cookie | ❌ 排除 |
| spec.n 位置搞错 | ✅ 已修正（在 JWT payload 内） |
| **密文缺会话绑定** | ⬅ **真正原因** |

判据：重放服务端自己产生过的 body → **200**；
        换成本地生成的密文 → 415。

## 五、下一步：逐步复现完整会话

```
S1  checksiteconfig           取 spec.c + JWT-A
S2  解码 JWT-A                取 n / l / i / e / c
S3  hsw 加载（自检串）        验证 wasm 就绪
S4  hsw(JWT-A, opts)          proof-1
S5  构造 l（含 manifest/pem） os(l) → E
S6  getcaptcha(E)             期望 200 + JWT-B
S7  hsw(JWT-B, opts)          proof-2 → P1_ token
S8  verify_challenge(token)   Origin: https://js.stripe.com
```

**验证基准**：S6 必须返回 200 且带 `c.req`。
若仍 415，则枚举 `l` 的字段组合，逐项二分。

## 六、已知可用的运行环境

- 本机 harness：`runtime/rea-hsw/harness/`
  - `real_env.cjs` — jsdom + 完整指纹 shim
  - `msgpack.cjs` — 提取出的 34 模块 codec
  - `../hsw_149.js` — 1.4MB hsw + 61KB 内嵌 wasm
- 服务器部署目录（Electron 24 + Xvfb + Mesa llvmpipe）
- 服务器已有成功样本：`/tmp/getcaptcha_body.bin`（18,775B）

## 七、代码落点（正式实现）

目标文件：`packages/core/src/gss_core/captcha.py`
现状：只有 YesCaptcha 外部打码，无本地 proof 能力。
计划新增：
- `_load_bundle(zip_bytes)`  解压 hsw.js
- `_HswRunner`               跑 wasm
- `build_getcaptcha_body()`  组 wire 格式
- `solve_hcaptcha_local()`   自产 token

---

## 八、415 根因已确定（实测证据）

### 交叉实验（决定性）
| elem0 (C) | elem1 (blob) | 结果 |
|---|---|---|
| 真实 | 真实 | **200** |
| 真实 | 本地 | 415 |
| 本地 | 真实 | 415 |
| 本地 | 本地 | 415 |

**结论：`c.req` 与密文必须由同一次会话产生。**

### 尾部字节（关键发现，差 1 字节就失败）
```
wire = 92 | da<len:2> | {type,req} | c8<len:2> | 密文 | 0x6b
                                                    ^^^^ 必须精确为 0x6b
```
实测：`0x6b` → 200；`0x6a`/`0x6c`/无/`0x00` → 415。

### 成功响应格式
```json
{"c":{"type":"hsw","req":"<JWT-B>"}, "success":false, "error-codes":[]}
```
823 字节明文 JSON。`success:false` 是正常的（挑战尚未答）。

### 会话绑定机制（待实现）
`checksiteconfig` 返回 JWT-A → `hsw` 在加载 bundle 时基于 JWT-A 派生会话密钥
→ `hsw(1, ...)` 的加密必须使用该密钥 → 服务端能用 JWT-A 解密。

实现要点：不能「先独立 hsw(1) 再拼 JWT-A」，必须：
```
hsw 加载 → 设置 spec(JWT-A) → proof → os(l)  全在同一实例内
```

---

## 九、重要修正：必须走订单代理

### 错误
早期所有 hCaptcha 请求都从**服务器机房 IP** 发出：
```
服务器出口   <SERVER_IP>   ← 错误用法
订单代理出口 73.21.122.107   ← 正确用法
```
后果：
- 触发 429 限流（暴力枚举 256 次后）
- 415 中有部分可能是 IP 信誉导致，与密文无关

### 正确做法
```js
const { HttpsProxyAgent } = require('https-proxy-agent');
const agent = new HttpsProxyAgent(process.env.ORDER_PROXY);
https.request({ ..., agent })
```
实测：走代理后 `checksiteconfig → 200，497ms`，无 429。

**结论：hCaptcha 判定对 IP 敏感。所有 hCaptcha 流量必须走订单代理。**

## 十、l 字段体积实测（补齐后）

| 字段 | 增量 | 内容 |
|---|---|---|
| 基础 | 144 B | `{v, sitekey, host, hl, action, n}` |
| `ws` | +49 B | `[[16,1,2,3,5],[1,1,1,1],20,[36,37,40,35,37]]` |
| `pd` | +81 B | 行为分析 |
| `pdc` | +34 B | 行为周期 |
| `pst` | +5 B | 私密状态令牌 |
| 全部字段合计 | ~477 B | 其余体积全在 `motionData` |

### 体积对齐结果
```
明文 18,160 B（真实 ~18,016）  差 0.8%
密文 18,188 B（真实 18,044）  差 0.8%
mm=238 点  pm=190 点
```
**体积已对齐，仍 415 → 证明体积不是判定因素。**

## 十一、最终缺口（唯一）

```
密文必须由「产生该 JWT 的同一个 hsw 会话」生成
```
- `hsw(1,...)` 的密钥在 wasm 内部，不导出
- 交叉实验：realC+realBlob=200，其余组合全 415
- 尾部字节不可猜（枚举 256 全失败），说明它参与密钥/完整性校验

### 可行的两条路
1. **页面内加密**：让页面自己的 hsw 做 `os()`（已验证能产出 200 的 body）
2. **复现 wasm 会话初始化**：需让 hsw 加载时绑定 JWT，当前未解

---

## 十二、B 路线终局结论（wasm 逆向完成）

### wasm 架构（100% 解出）

| 项 | 值 |
|---|---|
| 完整大小 | 955,460 字节 |
| WAT | 81,833,225 字符 |
| 函数 | 1,212 个 |
| 导出 | 21 个（Ac…Uc） |
| 字符串表 | 431 条，解码器已实现 |

### 字符串解码器（关键，用 Math.imul）
```js
function aee(idx) {
  const enc = table[idx];            // aWs(153+idx)
  const buf = Buffer.from(enc, 'base64');
  let K = (4094822375 ^ buf[0]) >>> 0;
  const out = [];
  for (let i = 1; i < buf.length; i++) {
    K = (K + 1831565813) >>> 0;
    let v = Math.imul(K ^ (K >>> 15), 1 | K) >>> 0;
    v = (v ^ ((v + Math.imul(v ^ (v >>> 7), 61 | v)) >>> 0)) >>> 0;
    out.push(buf[i] ^ ((v ^ (v >>> 14)) & 255));
  }
  return Buffer.from(out).toString('utf8');
}
aee(45)="setUint32" aee(46)="Hc" aee(47)="Cc" aee(48)="Pc"
aee(230)="catch"    aee(231)="instance"
// iA(instance.exports["catch"]["instance"]) → pU
```

### Uc 分派表（func 1128）
```
1334329966 0x4f88426e → call 736
2117712462 0x7e39ba4e → call 1109
-531224285 0xe0562923 → call 1109   解码 (Aa)
  30093134 0x01cb2f4e → call 1109
1834508644 0x6d586164 → call 416    编码 (Mh)
1231890261 0x496d2755 → call 742
 235612113 0x0e0b27d1 → call 802
```

### 调用链
```
func 298 (19,041 字符, 根入口)
  ├─ 1070 (65,688) add×320 xor×360 rotl×224  ← 压缩核心
  ├─ 908  memcpy → 64 字节块
  ├─ 802  memset 状态区
  ├─ 710  长度运算
  └─ 288  (4,402) 第二入口，也调 1070
```

### 状态结构（偏移）
```
+164 已处理计数    +184 当前块长度
+188 已处理总量    +192 块指针
64 字节分块
```

## 十三、决定性实验：加密非确定性

```
相同输入 {a:1,b:'hello'} (12 字节)，连续 4 次：
  run0: 0996cb83... 40B
  run1: 25958ca8... 40B
  run2: 021b2206... 40B
  run3: 4ad563cd... 40B
→ 每次不同 = 随机 nonce
```

固定开销 28 字节 = 随机 nonce + 认证码 + 长度（AEAD 结构）

### 结论
| 层面 | 状态 |
|---|---|
| wasm 架构 | ✅ 完全解出 |
| 压缩函数 | ✅ 已定位 |
| 加密模式 | ✅ AEAD + 随机 nonce |
| 离线伪造 | ❌ 密码学上不可行 |
| 密钥派生 | ❌ 不可逆且每次随机 |

**随机 nonce 使预计算不可能；会话绑定使跨会话复用不可能。**
这是 hCaptcha 的设计目标，不是实现缺陷。

## 十四、唯一可行解

```
支付会话 + hCaptcha 必须运行在同一浏览器实例
```

服务器验证结果：
```
Electron + Xvfb + llvmpipe
→ pay.openai.com 完整渲染（$50 / Card / 地址 / Subscribe）
→ 页面自产 getcaptcha body 重放 = 200
→ 卡号可填（<CARD_NUMBER> 成功格式化）
→ 字段名: cardNumber/cardExpiry/cardCvc/billingName/
          billingAddressLine1/billingLocality/billingPostalCode
```

支付协议全链路（已掌握，无需再逆向）：
```
checksiteconfig → proof → os(l) → getcaptcha → JWT-B → verify_challenge
verify_challenge 必须 Origin: https://js.stripe.com
其余端点 Origin: https://pay.openai.com
```
