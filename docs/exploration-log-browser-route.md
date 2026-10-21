# 浏览器路线探索记录（2026-10-20 ~ 10-21）

本文记录「如何通过 Stripe 的 `intent_confirmation_challenge`」这一问题的完整探索过程、
每一轮的实际数据、以及最终为什么放弃浏览器与打码路线、回到全协议。

技术细节与实测参数见 [hcaptcha-direct-solve.md](./hcaptcha-direct-solve.md)。

---

## 一、问题定义

OpenAI / Stripe 的托管结账（hosted checkout）在 confirm 阶段会返回三种结果之一：

```
① 402 card_declined                      ← 拿到卡片结果，流程结束
② 200 intent_confirmation_challenge      ← 需要过 hCaptcha 挑战
③ 200 succeeded
```

目标：让 confirm 稳定落到 ① 或 ③。

挑战涉及两个 hCaptcha sitekey：

| sitekey | 用途 | 实测表现 |
|---|---|---|
| `24ed0064-62cf-4d42-9960-5dd1a41d4e29` | passive（隐形、无图） | 可解，13–25s；指纹优化后 3–6s |
| `c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a` | 二次挑战（真图像题） | 有图/无图混出，第三方 token 一律被拒 |

---

## 二、阶段一：指纹自洽化

### 出发点

服务器（Linux VPS）跑 headless Chrome 求 passive token，但契约上 hCaptcha 会读大量指纹信号。
最初怀疑是「机房 IP + 软件渲染 GPU」导致评分低。

### 做过的尝试与结果

| 尝试 | 结果 |
|---|---|
| 伪装成 Windows（UA + platform + 字体） | 更差：WebGL 精度、canvas 哈希、字体列表全是 Linux 真值，交叉验证露馅 |
| 改为如实报告 Linux（UA 也改 Linux） | **有效**：passive 从 27.1s 降到 5.8s（4.7 倍） |
| 装 401 个字体 + 字体别名映射 | 字体数 6 → 13，作为自洽性补强 |
| `m.stripe.com/6` × 4 完整指纹注册 | 对挑战无效（仍 100% 下发） |

### 关键教训

**「自洽的真话」比「矛盾的谎话」评分更高。** 我们把 UA / platform / userAgentData /
字体 / WebGL 全部对齐到真实的 Linux Chrome，passive 提速 4.7 倍；而伪装 Windows 反而更差。

### 引入 Camoufox

Firefox 内核的指纹伪装框架（MPL-2.0）。实测指纹：

```
mediump 精度       23/127/127   （headless Chrome 只有 10/15/15）
screen / avail     1920x1080 / 1920x1053
outer / inner      1920x1053 / 1920x967
webdriver          false
passive 求解       4.0–8.0s
```

`mediump` 这一项是 headless Chrome 在 JS 层无法伪造的（真实 Mesa llvmpipe 值），
Camoufox 在 C++ 层解决了它。

**但对 `c7faac4c` 无效**：照样出图像题。

---

## 三、阶段二：求解器工程

既然要过图像题，就自己渲染、自己点击。做了完整的交互链路。

### 踩过的坑（全部已修）

| 问题 | 根因 | 修法 |
|---|---|---|
| `window.__reaSolve is not a function` | Camoufox 的 `page.evaluate` 跑在**隔离世界**，读不到主世界 `window` | 页面自驱动：从 URL query 取参数，结果写进 `<pre id="result">`，Python 只轮询 DOM |
| 点击落在格子缝隙 | 3×3 系数靠猜 | 用 `drawImage` 的落点参数精确计算 |
| 九宫格切分不准 | 对着放大后的 canvas 切，还带 240px 黑边 | 拦截原始合成图（480×320，严丝合缝），按 3×3 均分 |
| `submit` 找不到按钮 | 按钮文案是 `Next`/`Verify`，且在跨域 frame 里 | 遍历所有 `challenge\|newassets` frame 匹配 |
| token 拿不到 | `readPendingToken()` 只读一次 | 改成 `waitToken()` 轮询 |
| 多页题只答一页 | 挑战有 `page 1 of 2` | `/answer` 改为按页循环 |

### 突破性发现：拦截 `drawImage` 拿原图

hCaptcha 把**解密后的原图**通过 `drawImage` 画到挑战 canvas 上。在每个新文档注入拦截器后：

```
网格题      1 张 480×320 合成图（3 行 3 列，无缝隙）
拖拽题      1 张 480×320 背景 + 2 张 85×85 可拖小块
```

而 canvas 是 1000×940 的**放大品**，上方还有 240px 黑边。
所以「对着 canvas 切分」本身就是错的方向 —— 原图一直都在。

### 阶段结论

完整链路跑通了：

```
判题 → 点击/拖拽 → 提交 → 换页 → 再判题 → 提交 → 等 token
实际输出：page1 提交 Next；page2 提交 Verify
```

**但始终拿不到 token**，因为答案虽对、点击也生效，挑战仍未通过。

---

## 四、阶段三：打码服务

### 路线 A：代拿 token（`HCaptchaTask`）

按官方文档把参数全部填对，六次组合实测：

| # | websiteURL | isInvisible | isEnterprise | rqdata 位置 | 出口 | 结果 |
|---|---|---|---|---|---|---|
| 1 | HCaptcha.html | false | – | 顶层 | proxyless | ❌ |
| 2 | HCaptcha.html | false | – | 顶层 | proxied | 代理超时 |
| 3 | pay.openai.com | false | – | 顶层 | proxyless | ❌ |
| 4 | pay.openai.com | true | – | 顶层 | proxied | ❌ |
| 5 | HCaptchaInvisible.html | true | – | 顶层+enterprisePayload | proxied | ❌ |
| 6 | HCaptchaInvisible.html | true | true | 顶层+enterprisePayload | proxied | ❌ |

全部返回 `payment_intent_authentication_failure` / `Captcha challenge failed`。

免费探测排除的参数问题：

```
verify_challenge 附加字段：guid/muid/sid/eid/challenge_response_key/captcha_key/ekey
                        等 10 个候选全部 parameter_unknown
确认：只接受 challenge_response_token
```

也排除的实现问题：

```
代理 IP 漂移      固定 sid 3 分钟内恒为 <PROXY_IP>  → 排除
rqdata 未注入     传非法 rqdata 会报 ERROR_CAPTCHA_UNSOLVABLE（失败退点）→ 证明已注入
```

### 路线 B：代答题（`HCaptchaClassification`，8 点/次）

这条技术上最接近成功，答案准确率很高。落盘核对过两次：

```
示例 = 帆船 → 9 格里唯一水上的是索引 2 → objects[2]=true, confidences[2]=0.9991  ✅
示例 = 小船 → 索引 5 是船 → confidences[5]=0.9994                                ✅
```

**服务商没答错。** 问题在我们点击坐标（用错画布）和题目本身的难度上。

### 成本

```
起始 11220 点 → 现在 10911 点
累计消耗约 309 点 ≈ 0.31 元（含 60 点是我早期错误探测）
```

---

## 五、阶段四：全协议验证

### confirm 不需要浏览器

```
省略 passive_captcha_token  → 200 → intent_confirmation_challenge
传空串                      → 200 → intent_confirmation_challenge
传真实 token（Camoufox）    → 200 → intent_confirmation_challenge
```

**纯 HTTP 即可走到 confirm。** 浏览器只影响能否过挑战。

### confirm 参数集锁死

想用 SCA 豁免参数绕过，全部被拒：

```
off_session=true                                        → parameter_unknown
payment_method_options[card][request_three_d_secure]    → parameter_unknown
payment_method_options[card][moto]                      → parameter_unknown
setup_future_usage=off_session                          → parameter_unknown
```

### `rv_timestamp` 逆向

从 605 条记录的 HAR 里提取真实 confirm 请求体，逐字段对比，唯一差异是 `rv_timestamp`
（212 字符、43 个字符的字符集）。

逆向过程：

```
131 个 HAR JS 全下载 → 只有 checkout-app-init 和 controller 提到它
                      → 两处都只是透传/声明，不是生成逻辑
init 响应里 → 没有该字段，也没有同字符集的其它值
按 43 字符集 + 212 长度搜索全部 bundle → 命中 0
```

结论：生成器在混淆/动态加载模块里，静态分析挖不出来。
而且**它不是挑战开关**（补齐后 4/4 仍出挑战，传随机值不报任何字段错误）。

### 卡片才是真正的变量

用 18 张虚拟卡跑纯协议：

```
4 张  ★ card_declined / fraudulent      ← 无挑战，直接拿到结果
8 张  intent_confirmation_challenge
3 张  CA 卡需加拿大地址（改用后报 checkout_amount_mismatch，需先 update_address）
```

拒卡率从「一直用同一张卡测」的 6% 提升到 **33%**。

---

## 六、最终结论

### 为什么放弃浏览器与打码

```
1. 挑战 token 必须在持有该次 rqdata 的会话内签发
   → 第三方代拿的 token 结构性无效（六次组合已排除所有参数可能）

2. 挑战 100% 下发，无法靠降低风险评分规避
   → 指纹、UA、rv_timestamp、passive token 全部对齐后仍是 100%

3. 会话内答题的链路已完整跑通，但答案始终未被接受
   → 投入产出比不如直接跑卡池

4. 卡片 BIN 才是可用的筛选维度
   → 33% 的卡在纯协议下直接拒卡，零成本
```

### 现在的形态

```
建单 → init → pm → confirm（纯 HTTP，无浏览器、无打码）
  ├─ 部分卡 → card_declined    ← 拿到卡片结果
  └─ 其余   → challenge        ← 直接丢弃，成本为零
```

服务栈：`gss-api`（订单/支付）+ `gss-window`（调度），无浏览器进程。

### 保留下来的资产

浏览器与打码代码全部移入 `attic/browser-route-20261021/`，其中可复用的实现：

| 文件 | 可复用内容 |
|---|---|
| `hcaptcha-solver/server.js` | `drawImage` 拦截拿原图、多页循环、`waitToken` 轮询、坐标换算、题型分派 |
| `camoufox-solver/server.py` | 隔离世界规避（DOM 通信）、指纹自洽的 Firefox 启动参数 |

### 如果将来要重开这条路

两个方向：

1. **换商户/换结账流程** —— 挑战触发率是商户侧 Radar 规则决定的，
   找一家不强制挑战的商户，问题直接消失
2. **补 `update_address`** —— 让 CA/GB 卡不再报 `checkout_amount_mismatch`，
   扩大可跑卡的覆盖范围
