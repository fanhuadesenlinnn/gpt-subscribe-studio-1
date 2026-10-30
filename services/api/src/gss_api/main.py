"""FastAPI 应用入口。默认只监听回环地址。"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gss_core import install_logging_filter
from gss_core.config import load_settings, missing_upstreams, use_fake_adapters
from gss_core.proxies import GatewayStore

from .routers import events, orders, promo, proxies, saved_cards, protocol_cards

__version__ = "0.1.0"


def _ensure_info_logging() -> None:
    gss = logging.getLogger("gss")
    gss.setLevel(logging.INFO)
    if not gss.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        gss.addHandler(handler)
    gss.propagate = False


def create_app() -> FastAPI:
    install_logging_filter()
    _ensure_info_logging()
    install_logging_filter(logging.getLogger("gss"))
    settings = load_settings()

    app = FastAPI(
        title="gpt-subscribe-studio API",
        version=__version__,
        description="独立的 ChatGPT 订阅开通工具：粘贴 session，浏览器填卡。",
    )
    app.include_router(orders.router)
    app.include_router(saved_cards.router)
    app.include_router(protocol_cards.router)
    app.include_router(promo.router)
    app.include_router(events.router)
    app.state.gateway_store = GatewayStore()
    app.include_router(proxies.router)

    @app.get("/api/health", tags=["health"])
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "promo_enabled": settings.enable_promo_mode,
            "fake_adapters": use_fake_adapters(),
            "missing_upstreams": missing_upstreams(settings),
        }

    # ── 出口代理连接失败 → 503（而不是裸 500）────────────────────
    # 住宅动态代理偶发 TLS 中断：BoringSSL SSL_connect: Connection closed abruptly。
    # 这类错误对用户意味着「换个出口重试」，不是服务端 bug。
    try:
        from curl_cffi.requests.exceptions import RequestException as _CurlErr
    except Exception:  # noqa: BLE001
        _CurlErr = None  # type: ignore[assignment]
    try:
        import httpx as _httpx
        _HttpxErr = _httpx.HTTPError
    except Exception:  # noqa: BLE001
        _HttpxErr = None  # type: ignore[assignment]

    _CONN_HINTS = (
        "SSL", "Connection closed", "ConnectError", "ConnectionError",
        "CurlError", "Temporary failure", "timed out", "Timeout",
        "RemoteDisconnected", "EOF", "reset by peer",
    )

    @app.exception_handler(Exception)
    async def _conn_error_handler(request, exc):  # noqa: ANN001
        is_conn = (
            (_CurlErr is not None and isinstance(exc, _CurlErr))
            or (_HttpxErr is not None and isinstance(exc, _HttpxErr))
            or any(k in str(exc) for k in _CONN_HINTS)
        )
        if is_conn:
            return JSONResponse(
                status_code=503,
                content={"detail": "出口代理连接 chatgpt.com 失败（住宅代理断连），"
                                   "请重试；连续失败请更换代理 SID 或线路。"},
            )
        raise exc

    return app


app = create_app()
