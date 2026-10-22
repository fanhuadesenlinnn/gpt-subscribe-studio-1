# 环境坑：两份源码树

服务器上有**两份** `gss_core`：

| 路径 | 用途 |
|---|---|
| `/opt/gss/src/packages/core/src` | Docker 镜像构建源（rsync 目标） |
| `/opt/gss/app/packages/core/src` | 宿主机脚本 import 的副本（venv 里的 sys.path 指向它） |

**rsync 只更新前者**。宿主机上直接跑的测试脚本（`/tmp/*.py`）导入的是**后者**，
于是一直在跑旧代码 —— 表现为「日志里的字符串在容器代码里根本不存在」。

## 修法

每次改完 `packages/core` 后同步两份：

```bash
cp /opt/gss/src/packages/core/src/gss_core/stripe_protocol.py \
   /opt/gss/app/packages/core/src/gss_core/stripe_protocol.py
```

## 另一个坑：求解器 URL

- 容器内 → `http://host.docker.internal:8197`
- 宿主机脚本 → `http://127.0.0.1:8197`

`/opt/gss/app/.env` 里配的是前者（给容器用），宿主机脚本必须显式覆盖：

```bash
GSS_LLM_SOLVER_URL=http://127.0.0.1:8197
```

## 修好之后的实测

```
⑦b 命中 challenge（第 1 次）site_key=c7faac4c
   解出 token（4770 字符），继续 verify     ← 27 秒解出
⑦c verify_challenge
   → 200 (2690 chars)
判定 captcha_rejected code=payment_intent_authentication_failure
```

**这是求解器第一次真正在主流程里跑通**（此前 16 轮全被旧代码挡住，一直在空转）。
剩下的是 token 被 Stripe 拒收 —— 即会话归属问题，见 README 的方案。

## handleNextAction 进展（第 19 轮）

已打通：
- 关掉协议内求解器（`GSS_LLM_SOLVER_URL=`）后，协议停在挑战处，
  `confirm` 响应里的 `client_secret` 可以拿到（`pi_xxx_secret_xxx`，60 字符）
- 这是 `stripe.handleNextAction({clientSecret})` 需要的输入

当前阻塞：
- 拿到 client_secret 后进入浏览器阶段，脚本卡在 `page.goto("https://example.com")`
  直到 620s 超时被杀，连 "已调用 handleNextAction" 都没打出来
- 说明 playwright + 代理的首次导航挂了（代理抖动 / Chrome 启动慢）

下一轮排查顺序：
1. 先用**不带代理**的 context 导航 example.com，确认浏览器本身能起
2. 再带代理，把 goto 超时压到 20s 并重试 3 次
3. 若 example.com 不稳，改用 `page.set_content` 直接写入（不需要导航）
   —— 但 Stripe.js 需要 hcaptcha/stripe 的 iframe 能加载，
   所以 origin 需要是真实域名，可用 `route()` 拦截任意域名返回我们的 HTML
4. 每步都 print + `python -u`，避免再被缓冲坑掉
