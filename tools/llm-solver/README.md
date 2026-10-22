# LLM 打码求解器（Team 协议模式专用）

用浏览器 + 多模态大模型解 Stripe `intent_confirmation_challenge`（hCaptcha `c7faac4c`）。

**已实测通过**：题面 `Find all things with on and off states` → LLM 判 `[0,4]` →
点格子 → 点 Verify → 拿到 4823 字符 token → `verify_challenge` 返回 **HTTP 200 且无 error**。

---

## 为什么需要它

纯协议链路命中 `intent_confirmation_challenge` 后只有一条路：在**持有该次 rqdata 的会话内**
把图像题答对。第三方代拿的 token 结构性无效（见 `docs/exploration-log-browser-route.md`）。

打码服务（`HCaptchaClassification`）能答客观题，但对「哪辆车停错了」「找出有开关状态的东西」
这类**语义题**答不了 —— 实测它给的点是错的，而 LLM 能答对。

---

## 依赖

```bash
python3 -m venv venv
./venv/bin/pip install hcaptcha-challenger playwright pillow httpx
./venv/bin/pip install camoufox        # 可选，指纹更好
```

浏览器：系统 Chrome（`/usr/bin/google-chrome`）+ Xvfb（`DISPLAY=:99`）。

---

## 两个必须打的补丁

### 1. Anthropic 兼容中转

`hCaptcha Challenger` 只内置 Gemini 原生 SDK 的 provider。中转若是 Anthropic 兼容协议，
需要 `tools/internal/providers/anthropic_compat.py`（本仓库 `attic/browser-route-20261021/`
同目录下有一份），并在 `tools/internal/base.py` 的 `_create_default_provider` 里按
环境变量分流。

### 2. `AgentConfig` 强制校验 `GEMINI_API_KEY`

即使走别家 provider，`AgentConfig` 也要求该变量非空 —— 填任意非空值即可。

---

## 环境变量

| 变量 | 说明 |
|---|---|
| `GSS_LLM_BASE_URL` | 中转地址，如 `https://api.example.com` |
| `GSS_LLM_KEY` | key |
| `GSS_LLM_MODEL` | 模型名，默认 `gemini-3.8-flash-high` |
| `DISPLAY` | `:99`（Xvfb） |

---

## 运行

```bash
cd /opt/gss/app && set -a && . .env && set +a
export DISPLAY=:99 GSS_LLM_BASE_URL=... GSS_LLM_KEY=... GSS_LLM_MODEL=...
/path/to/venv/bin/python tools/llm-solver/solve.py
```

输出 `verify_challenge` 的 HTTP 码与错误字段。`HTTP 200` + `error: None` 表示挑战已过。

---

## 关键实现点（踩过的坑）

**1. 取图不要 fetch，也不要截整屏**

- `fetch(imgs.hcaptcha.com)` 会被 CORS 挡
- 截 canvas 会拍到大片黑边（canvas 是 1000×940 的放大品，顶部约 240px 空白）
- 正确做法：元素截图 `div[role=button][aria-label*="Challenge Image"]`，
  或拦 `CanvasRenderingContext2D.prototype.drawImage` 拿原始位图

**2. 选择器必须带 aria-label**

`div[role=button]` 会同时匹配到 `Skip` / 刷新 / `EN` 这些控制按钮，
截出来的「格子图」其实是按钮图标 —— LLM 看到 Skip 当然答不出。

**3. 点击坐标按位置排序**

`aria-label` 的数字不一定是网格顺序，按 `boundingBox` 的 `(y, x)` 排序才稳。

**4. hCaptcha 会先插一个 `display:none` 的空壳 iframe**

用 `page.$('iframe[src*="frame=challenge"]')` 取第一个匹配会拿到空壳，
`boundingBox` 返回 `y=-9999`，所有点击都落到视口外。
遍历所有候选取「可见且宽高 ≥ 50」中面积最大的那个。

---

## 与打码服务的关系

| 题型 | 打码服务 | LLM |
|---|---|---|
| 九宫格客观题（找出所有船） | ✅ 准确率高（实测置信度 0.999） | ✅ |
| 点击题（点所有空车位） | ✅ 实测正确 | ✅ |
| 语义题（哪辆车停错了 / 有开关状态的东西） | ❌ 答不对 | ✅ |
| 拖拽题 | 需单独格式 | ✅ 框架原生支持 |

**成本对比**：打码服务 8 点/页；LLM 走中转，按 token 计费。
本轮实测解一题 1 次调用，打码余额 **0 消耗**。
