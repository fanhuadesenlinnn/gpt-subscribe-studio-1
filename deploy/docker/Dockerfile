# 纯协议版 gss-api 容器镜像
#
# 浏览器路线归档后（见 attic/browser-route-20261021/），运行期不再需要
# Chrome / Xvfb / 证书 / puppeteer。
#
# 但仍需要 **Node.js**：packages/core/src/gss_core/sentinel.py 通过 subprocess
# 调用 sentinel_assets/sentinel_bridge.js 来签发 ChatGPT 的 Sentinel 风控令牌。
# 那不是浏览器，是一段纯 JS（浏览器 shim + 混淆 sdk.js + PoW），跑在 Node 里。
#
# 故采用多阶段：从 node 官方镜像取 node 二进制与 undici 依赖，
# 最终镜像仍是干净的 python:3.12-slim。

# ── 阶段 1：准备 node 与 undici ──────────────────────────
FROM node:20-bookworm-slim AS nodeenv

# sentinel_bridge.js 需要 undici 才能把 fetch 与 ProxyAgent 配成同一版本；
# 无代理时可退回 Node 内置全局 fetch，但建单链路会走代理，所以必须装。
RUN mkdir -p /opt/nodedeps \
 && cd /opt/nodedeps \
 && npm init -y >/dev/null 2>&1 \
 && npm install --omit=dev --no-audit --no-fund undici@6 >/dev/null 2>&1 \
 && node -e "require('/opt/nodedeps/node_modules/undici'); console.log('undici ok')"

# ── 阶段 2：运行镜像 ────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# node 二进制 + undici 依赖
COPY --from=nodeenv /usr/local/bin/node /usr/local/bin/node
COPY --from=nodeenv /opt/nodedeps/node_modules /app/node_modules
ENV NODE_PATH=/app/node_modules

# 依赖层单独缓存：requirements 不变时不会重装
COPY deploy/server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 代码：packages 与 services 都是「src 布局」，靠 PYTHONPATH 暴露
COPY packages/ /app/packages/
COPY services/ /app/services/
COPY deploy/server/app.py /app/app.py
COPY deploy/server/login.html /app/login.html
COPY apps/web/dist/ /app/web/

ENV PYTHONPATH=/app/packages/contracts/src:/app/packages/core/src:/app/services/account/src:/app/services/checkout/src:/app/services/payment/src:/app/services/api/src:/app/services/orchestrator/src \
    GSS_WEB_DIR=/app/web \
    GSS_LOGIN_FILE=/app/login.html \
    GSS_DATA_DIR=/data \
    GSS_CONFIG_DIR=/config \
    GSS_AUTH_FILE=/config/container-auth.json \
    GSS_BIND_HOST=0.0.0.0 \
    GSS_BIND_PORT=18120

EXPOSE 18120

# 根路径会 303 跳到登录页，能拿到响应即视为存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS -o /dev/null -L "http://127.0.0.1:${GSS_BIND_PORT}/" || exit 1

CMD ["sh", "-c", "exec python -m uvicorn app:app --host ${GSS_BIND_HOST} --port ${GSS_BIND_PORT}"]
