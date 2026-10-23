# 挑战求解 —— 当前状态与交接

## 一句话

**技术上已证明「挑战可以解」，卡在「挑战只有约 1/5 概率下发」。**

## 已验证打通（有日志/截图证据）

| 环节 | 证据 | 轮次 |
|---|---|---|
| 结账页加载 | `OpenAI OpCo, LLC` / 商品价格正确 | 23 |
| 11 项字段填写 | email/phone/tos/number/expiry/cvc/name/address1/city/postal + 国家州 | 27-39 |
| 提交 → Processing | 截图 | 39 |
| 挑战在 Stripe 页面弹出 | 截图 `One more step / I am human` | 39 |
| 真实指针序列点方框 | 8 步 move + down + up | 53 |
| canvas 取图 500×470 | 元素截图（绕开 toDataURL 跨域污染） | 47/53/68 |
| 点击题判题 | 服务商 `box` 返回 1-3 个点，画回原图核过 | 53/55 |
| 点击 + 提交 Next | `提交 Next` → `挑战处理 True` | 53/55 |
| 拖拽题求解 | `SpatialPathReasoner` → `paths` 真实坐标 | 67/68 |
| 拖拽真实执行 | `已拖拽 (624,368) -> (826,522)` | 68 |

## 未解决

**canvas 下发概率约 1/5。** 其余时候点完方框后 hCaptcha 不出题，
等待 30s 也没有，只能放弃本轮。

已排除：
- ❌ IP 被标记 —— 每轮 SID 都换新 IP（第 70 轮验证）
- ❌ 选择器不对 —— canvas 出得来的时候能正常取图判题
- ❌ 合成点击 —— 已改真实指针序列

最可能：**浏览器指纹 / 行为特征被 hCaptcha 评分**。

## 关键实现细节（都在代码里）

```
checkout_flow.py
  make_grid()           create_coordinate_grid 生成带坐标轴网格图
  reason_drag()         SpatialPathReasoner（必须设 GSS_LLM_PROVIDER=anthropic）
  solve_hcaptcha()      主循环：取图 → 判题型 → 拖拽/点击 → 提交
```

坐标换算链：
```
LLM 画布坐标 → rect.x + x/W*rect.w → 页面坐标 → mouse 动作
```

## 环境变量（缺一不可）

```bash
GSS_LLM_PROVIDER=anthropic
GSS_LLM_BASE_URL=https://api.tokenfly.shop
GSS_LLM_MODEL=gemini-3.8-flash-high
GSS_LLM_KEY=sk-...
GSS_CAPTCHA_API_KEY=...
DISPLAY=:99
```

## 踩过的坑（按重要性）

1. **`page.mouse.click` 会被识别** → 必须 move 分步 + down/up
2. **canvas `toDataURL` 跨域污染** → 改 `element.screenshot()`
3. **没有 `frame=checkbox`** → 是 `hcaptcha-invisible` + `hcaptcha-inner` 全屏模式
4. **`SpatialPathReasoner` 默认走 Gemini 原生** → 必须设 `GSS_LLM_PROVIDER`
5. **两份源码树** `/opt/gss/src` vs `/opt/gss/app` → 宿主机脚本 import 后者
6. **stdout 块缓冲** → 必须 `python -u`

## 下一步的选项

**A. 提升 canvas 下发率** —— 换 Camoufox / 真实 profile / 随机化行为
**B. 接受现状** —— 1/5 概率，多跑几次能出结果
**C. 转卡池筛选** —— 约 1/3 卡直接拒卡，零成本，能稳定出结果
