# hCaptcha solver —— 容器镜像
#
# 构建：
#   docker build -t gss-hcaptcha-solver .
#
# 运行：
#   docker run -d --name hcaptcha-solver \
#     -p 127.0.0.1:8199:8199 \
#     --shm-size=1g \
#     --security-opt seccomp=unconfined \
#     gss-hcaptcha-solver

FROM node:20-slim

ENV DEBIAN_FRONTEND=noninteractive

# Chrome 依赖 + Xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      xvfb \
      fonts-liberation \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libatspi2.0-0 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libglib2.0-0 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY server.js ./

ENV CHROME_PATH=/usr/bin/google-chrome \
    DISPLAY=:99 \
    PORT=8199 \
    HOST=0.0.0.0

# Xvfb 与 solver 同容器启动
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -e' \
      'Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &' \
      'sleep 1' \
      'exec node server.js' \
      > /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

EXPOSE 8199

HEALTHCHECK --interval=30s --timeout=60s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8199/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
