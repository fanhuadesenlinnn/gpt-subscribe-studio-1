"""订单凭证金库：session / Stripe 上下文只在进程内存，任务结束后可丢弃。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OrderSecrets:
    session: str
    account_id: str = ""
    checkout_session_id: str = ""
    publishable_key: str = ""
    setup_intent_id: str = ""
    setup_client_secret: str = ""
    attestation: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    proxy: str = ""
    # 建单时选定的指纹 profile，支付与求解器必须复用同一份
    profile: Optional[Dict[str, Any]] = None
    billing: Dict[str, str] = field(default_factory=dict)
    team_context: Optional[Dict[str, Any]] = None
    saved_card_context: Optional[Dict[str, Any]] = None
    expires_at: datetime = field(default_factory=lambda: _utcnow() + timedelta(minutes=30))


class SecretVault:
    """按订单号暂存凭证。不写磁盘、不进日志。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._items: Dict[str, OrderSecrets] = {}

    def put(self, order_id: str, secrets: OrderSecrets) -> None:
        with self._lock:
            self._purge()
            self._items[order_id] = secrets

    def get(self, order_id: str) -> Optional[OrderSecrets]:
        with self._lock:
            self._purge()
            return self._items.get(order_id)

    def drop(self, order_id: str) -> None:
        with self._lock:
            self._items.pop(order_id, None)

    def _purge(self) -> None:
        now = _utcnow()
        dead = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in dead:
            self._items.pop(key, None)


vault = SecretVault()

__all__ = ["OrderSecrets", "SecretVault", "vault"]
