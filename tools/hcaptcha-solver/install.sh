#!/usr/bin/env bash
# hCaptcha solver 一键安装（Linux）
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 检查 Node.js"
if ! command -v node >/dev/null 2>&1; then
  echo "错误：未找到 node。请先安装 Node.js >= 18"
  echo "  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
  echo "  sudo apt-get install -y nodejs"
  exit 1
fi
node -v

echo "==> 检查 Chrome"
CHROME_BIN="${CHROME_PATH:-/usr/bin/google-chrome}"
if [ ! -x "$CHROME_BIN" ]; then
  if command -v chromium >/dev/null 2>&1; then
    CHROME_BIN="$(command -v chromium)"
  elif command -v chromium-browser >/dev/null 2>&1; then
    CHROME_BIN="$(command -v chromium-browser)"
  else
    echo "错误：未找到 Chrome/Chromium"
    echo "  Debian/Ubuntu:"
    echo "    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    echo "    sudo apt install -y ./google-chrome-stable_current_amd64.deb"
    exit 1
  fi
fi
echo "Chrome: $CHROME_BIN"

echo "==> 检查 Xvfb"
if ! command -v Xvfb >/dev/null 2>&1; then
  echo "未找到 Xvfb，尝试安装..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y xvfb
  else
    echo "请手动安装 Xvfb 后重试"
    exit 1
  fi
fi

echo "==> 安装依赖"
npm install --omit=dev

echo "==> 生成启动脚本"
cat > start.sh <<START
#!/usr/bin/env bash
set -euo pipefail
cd "\$(dirname "\$0")"

# 启动 Xvfb（如果没在跑）
if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
  sleep 1
fi

export DISPLAY=:99
export CHROME_PATH="${CHROME_BIN}"
export PORT="\${PORT:-8199}"
exec node server.js
START
chmod +x start.sh

echo
echo "安装完成。启动："
echo "  ./start.sh"
echo
echo "验证："
echo "  curl -s http://127.0.0.1:8199/health"
