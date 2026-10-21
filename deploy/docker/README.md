# Docker 部署（纯协议版）

浏览器路线归档后，运行期不再需要 Chrome / Xvfb / 证书，容器化重新变得可行。
本文是**线上实际使用**的部署方式；裸机版说明保留在上一级 `README.md` 作为备份方案。

---

## 一、为什么现在能容器化

之前的阻碍是 hCaptcha 求解器需要：

```
Xvfb :99 + 无头 Chrome + 自签证书 + node 服务 + 常驻 646MB 的 Firefox
```

浏览器路线终止后（见 `docs/exploration-log-browser-route.md`），剩下的运行期依赖是：

| 依赖 | 用途 | 是否必需 |
|---|---|---|
| Python 3.12 + requirements.txt | API 主体 | ✅ |
| **Node.js 20** | `sentinel.py` 调 `sentinel_bridge.js` 签发 ChatGPT Sentinel 令牌 | ✅ |
| `undici`（npm） | Sentinel 桥在有代理时把 fetch 与 ProxyAgent 配成同一版本 | ✅ |
| Chrome / Xvfb / 证书 | hCaptcha 求解器 | ❌ 已归档 |

**注意 Node 仍然是必需的**：它不是浏览器，是一段纯 JS 在跑（浏览器 shim + 混淆 sdk.js + PoW）。
建单接口返回过这个错误，就是这个原因：

```json
{"code":"checkout_upstream_error",
 "message":"无法签发 ChatGPT 风控令牌（Sentinel）。请确认容器里有 Node，且出口代理可用。"}
```

---

## 二、镜像设计

`Dockerfile` 用多阶段构建：

```
阶段 1  node:20-bookworm-slim   →  产出 node 二进制 + /opt/nodedeps/node_modules(undici)
阶段 2  python:3.12-slim        →  拷入 node 与 undici，装 Python 依赖，拷代码
```

`NODE_PATH=/app/node_modules` 让 `sentinel_bridge.js` 里的 `require("undici")` 能解析到。

最终镜像里不含 npm、不含构建缓存，也不含任何浏览器。

---

## 三、目录与挂载

代码从仓库构建，运行时产物挂载宿主机目录：

```
/opt/gss/src     仓库源码（构建上下文）
/opt/gss/data    → /data      at_list.json / window_state.json 等运行数据
/opt/gss/config  → /config    container-auth.json / proxy-gateways.json（只读）
/opt/gss/app/.env            通过 env_file 注入
```

镜像内的路径与 `app.py` 的默认值一致（`/app/web`、`/app/login.html`），
但 `.env` 里写的是宿主机路径，**必须用 compose 的 `environment:` 覆盖**，
否则启动时会报：

```
RuntimeError: Directory '/opt/gss/app/web' does not exist
```

---

## 四、部署

```bash
# 1) 同步源码
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '__pycache__' --exclude 'attic' --exclude 'runtime' \
  ./ root@<host>:/opt/gss/src/

# 2) 构建并启动
ssh root@<host>
cd /opt/gss/src
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 3) 停掉旧的裸机服务
systemctl stop gss-api && systemctl disable gss-api
```

端口只发布到 `127.0.0.1:18120`，对外仍由 caddy 反代，暴露面与裸机时一致。

---

## 五、验证清单

```bash
# 容器健康（镜像内 HEALTHCHECK 会走 / → 303）
docker ps --filter name=gss-api --format '{{.Status}}'
docker logs gss-api --tail 20

# 镜像内必需的运行时
docker exec gss-api node -v                      # v20.x
docker exec gss-api node -e 'require("undici")'  # 不报错

# API 可用
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18120/            # 303
/opt/gss/app/.venv/bin/python /tmp/verify_container.py                      # 建单 201

# 完整支付链路（纯协议）
cd /opt/gss/app && set -a && . .env && . ./cards.env && set +a
GSS_PUBLIC_ORIGIN=https://<PAY_HOST>t PLAN=team \
  .venv/bin/python /opt/gss/scripts/run_sp_test.py
```

实测输出：

```
订单: ae66e375
① m.stripe.com/6 注册指纹
② init
⑤ 跳过 passive captcha（纯协议）
⑥ payment_methods
⑦ confirm (hosted 流)
⑦b 命中 challenge（第 1 次）site_key=c7faac4c
判定 challenge_required
```

---

## 六、仍留在裸机的部分

`gss-window`（`/opt/gss/scripts/window_scheduler.py`）仍是 systemd 服务。
它只是个调度器，不引浏览器；要一起容器化，把 `scripts/` 纳入构建上下文即可。

---

## 七、回滚

```bash
cd /opt/gss/src && docker compose -f deploy/docker/docker-compose.yml down
systemctl enable --now gss-api
```

裸机 venv 与 systemd 单元都还在，回滚是即时的。
