"""订单存储。session / 卡字段绝不进入这里。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from gss_contracts import Job, Order, OrderStatus, SubscriptionInfo


@dataclass
class OrderView:
    """API 展示用的附加字段（不含凭证）。"""

    checkout_url: Optional[str] = None
    checkout_session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    customer_session_client_secret: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    last_error: Optional[str] = None
    quote_error: Optional[str] = None
    next_action: Optional[Dict[str, Any]] = None
    subscription: Optional[SubscriptionInfo] = None
    checkout_kind: Optional[str] = None
    seat_quantity: Optional[int] = None
    workspace_name: Optional[str] = None
    promo_code: Optional[str] = None
    promo_campaign_id: Optional[str] = None
    price_interval: Optional[str] = None
    confirm_return_url: Optional[str] = None
    protocol_saved_result: Optional[Dict[str, Any]] = None


class InMemoryStore:
    def __init__(self) -> None:
        self.orders: Dict[str, Order] = {}
        self.jobs: Dict[str, Job] = {}
        self.views: Dict[str, OrderView] = {}
        self._idempotency: Dict[str, str] = {}

    def find_by_idempotency(self, key: Optional[str]) -> Optional[Order]:
        if not key:
            return None
        order_id = self._idempotency.get(key)
        return self.orders.get(order_id) if order_id else None

    def create_order(self, order: Order, idempotency_key: Optional[str] = None) -> Order:
        self.orders[order.id] = order
        self.views.setdefault(order.id, OrderView())
        if idempotency_key:
            self._idempotency[idempotency_key] = order.id
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def view(self, order_id: str) -> OrderView:
        return self.views.setdefault(order_id, OrderView())

    def update_status(self, order_id: str, status: OrderStatus) -> Order:
        order = self.orders[order_id]
        order.status = status
        return order

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        return job

    def jobs_for_order(self, order_id: str) -> List[Job]:
        return [job for job in self.jobs.values() if job.order_id == order_id]

    def clear(self) -> None:
        self.orders.clear()
        self.jobs.clear()
        self.views.clear()
        self._idempotency.clear()


store = InMemoryStore()

__all__ = ["InMemoryStore", "OrderView", "store"]
