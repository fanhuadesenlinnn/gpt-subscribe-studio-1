"""自有服务器入口：同源提供订单 API 与编译后的前端。"""

import os

from fastapi.staticfiles import StaticFiles

from gss_api.main import create_app
from gss_api.test_access import TestAccessMiddleware

app = create_app()
app.add_middleware(
    TestAccessMiddleware,
    credentials_file=os.environ["GSS_AUTH_FILE"],
    login_file=os.environ.get("GSS_LOGIN_FILE", "/app/login.html"),
    origin=os.environ.get("GSS_PUBLIC_ORIGIN", ""),
)
app.mount("/", StaticFiles(
    directory=os.environ.get("GSS_WEB_DIR", "/app/web"), html=True
), name="web")
