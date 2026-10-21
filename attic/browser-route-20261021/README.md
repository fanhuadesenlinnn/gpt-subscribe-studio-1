# 浏览器与打码求解器（已停用备份）

本目录的文件于 2026-10-21 从 `tools/` 移入，原因是项目已切换为**纯协议模式**：
不出浏览器、不调打码服务。

## 为什么停用

条纹（Stripe）的 `intent_confirmation_challenge` 是一个**会话绑定的企业版 hCaptcha 挑战**。
实测结论（详见 `docs/hcaptcha-direct-solve.md` 第 29–32 节）：

1. 第三方打码服务代拿的 token 一律被判 `Captcha challenge failed`
   - 六种参数组合全部失败（proxied/proxyless × websiteURL × isInvisible × isEnterprise × rqdata 两种放置方式）
   - `verify_challenge` 请求体已确认完整：10 个候选字段全 `parameter_unknown`
2. 挑战 token 必须持有该次 `rqdata` 的会话内签发
3. 挑战本身 100% 下发，无法靠降低风险评分规避
   - 完整 `m.stripe.com/6` ×4 指纹注册无效
   - UA 全链路统一无效（但让 passive 从 27.1s 降到 5.8s）
   - `rv_timestamp` 补齐无效

## 保留了哪些可复用资产

| 目录 | 内容 | 复用价值 |
|---|---|---|
| `hcaptcha-solver/` | Node + Chrome 求解器（server.js 约 2100 行） | 内含 `drawImage` 拦截拿原图、多页循环、坐标换算等完整实现 |
| `camoufox-solver/` | Camoufox(Firefox) 求解器 | 指纹自洽（mediump 23/127/127，passive 3–8s） |
| `hcaptcha-bridge/` | 浏览器桥接 | — |
| `rea-chrome/` | Chrome 逆向调试台 | — |
| `auto-pay/` | 浏览器自动化支付 | — |

## 恢复方法

```bash
git mv attic/browser-route-20261021/hcaptcha-solver tools/
systemctl enable --now gss-solver
```
