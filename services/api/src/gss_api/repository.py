"""订单存储实现（SQLAlchemy 版本）。

替换骨架的 InMemoryStore，提供完整的持久化能力：
- 订单 CRUD 与状态迁移
- 幂等键去重
- 任务与支付尝试管理

宪法第 I 条：此处**不得**保存 session / token / 卡字段。
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from gss_contracts import (
    Job,
    Order,
    OrderStatus,
    PaymentAttempt,
    PaymentStatus,
    Plan,
    Mode,
    AccountSource,
    CardSource,
    BillingMode,
)

from .db import JobModel, OrderModel, PaymentAttemptModel, get_db


class OrderRepository:
    """订单存储层。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- 幂等 ---------------------------------------------------------------- #
    def find_by_idempotency(self, key: Optional[str]) -> Optional[Order]:
        """根据幂等键查找订单。"""
        if not key:
            return None
        stmt = select(OrderModel).where(OrderModel.idempotency_key == key)
        model = self.session.execute(stmt).scalar_one_or_none()
        return self._to_domain(model) if model else None

    # -- 订单 ---------------------------------------------------------------- #
    def create_order(
        self, order: Order, idempotency_key: Optional[str] = None
    ) -> Order:
        """创建订单。"""
        model = OrderModel(
            id=order.id,
            idempotency_key=idempotency_key,
            plan=order.plan,
            billing_country=order.billing_country,
            account_source=str(order.mode.account_source),
            card_source=str(order.mode.card_source),
            billing_mode=str(order.mode.billing_mode),
            risk_acknowledged=order.mode.risk_acknowledged,
            status=order.status,
            amount=order.amount,
            cost=order.cost,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._to_domain(model)

    def get_order(self, order_id: str) -> Optional[Order]:
        """根据 ID 查询订单。"""
        model = self.session.get(OrderModel, order_id)
        return self._to_domain(model) if model else None

    def update_status(
        self, order_id: str, status: OrderStatus, **kwargs
    ) -> Order:
        """更新订单状态及其他字段。"""
        model = self.session.get(OrderModel, order_id)
        if not model:
            raise ValueError(f"订单不存在: {order_id}")
        
        model.status = status
        for key, value in kwargs.items():
            if hasattr(model, key):
                setattr(model, key, value)
        
        self.session.commit()
        self.session.refresh(model)
        return self._to_domain(model)

    def list_orders(
        self,
        status: Optional[OrderStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Order]:
        """列出订单（支持状态过滤与分页）。"""
        stmt = select(OrderModel)
        if status:
            stmt = stmt.where(OrderModel.status == status)
        stmt = stmt.limit(limit).offset(offset).order_by(OrderModel.created_at.desc())
        
        models = self.session.execute(stmt).scalars().all()
        return [self._to_domain(m) for m in models]

    # -- 任务 ---------------------------------------------------------------- #
    def add_job(self, job: Job) -> Job:
        """创建编排任务。"""
        model = JobModel(
            id=job.id,
            order_id=job.order_id,
            stage=job.stage,
            attempts=job.attempts,
            locked_by=job.locked_by,
            locked_at=job.locked_at,
            last_error=job.last_error,
            checkout_url=job.checkout_url,
        )
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._job_to_domain(model)

    def get_job(self, job_id: str) -> Optional[Job]:
        """根据 ID 查询任务。"""
        model = self.session.get(JobModel, job_id)
        return self._job_to_domain(model) if model else None

    def jobs_for_order(self, order_id: str) -> List[Job]:
        """查询订单的所有任务。"""
        stmt = (
            select(JobModel)
            .where(JobModel.order_id == order_id)
            .order_by(JobModel.created_at.asc())
        )
        models = self.session.execute(stmt).scalars().all()
        return [self._job_to_domain(m) for m in models]

    def update_job(
        self, job_id: str, **kwargs
    ) -> Job:
        """更新任务字段。"""
        model = self.session.get(JobModel, job_id)
        if not model:
            raise ValueError(f"任务不存在: {job_id}")
        
        for key, value in kwargs.items():
            if hasattr(model, key):
                setattr(model, key, value)
        
        self.session.commit()
        self.session.refresh(model)
        return self._job_to_domain(model)

    # -- 支付尝试 ------------------------------------------------------------ #
    def add_payment_attempt(self, attempt: PaymentAttempt) -> PaymentAttempt:
        """记录支付尝试。"""
        model = PaymentAttemptModel(
            id=attempt.id,
            order_id=attempt.order_id,
            provider=attempt.provider,
            status=attempt.status,
            next_action=attempt.next_action,
            proxy_role=attempt.proxy_role,
        )
        self.session.add(model)
        self.session.commit()
        self.session.refresh(model)
        return self._payment_to_domain(model)

    def get_payment_attempts(self, order_id: str) -> List[PaymentAttempt]:
        """查询订单的所有支付尝试。"""
        stmt = (
            select(PaymentAttemptModel)
            .where(PaymentAttemptModel.order_id == order_id)
            .order_by(PaymentAttemptModel.created_at.desc())
        )
        models = self.session.execute(stmt).scalars().all()
        return [self._payment_to_domain(m) for m in models]

    def update_payment_status(
        self, attempt_id: str, status: PaymentStatus, **kwargs
    ) -> PaymentAttempt:
        """更新支付尝试状态。"""
        model = self.session.get(PaymentAttemptModel, attempt_id)
        if not model:
            raise ValueError(f"支付尝试不存在: {attempt_id}")
        
        model.status = status
        for key, value in kwargs.items():
            if hasattr(model, key):
                setattr(model, key, value)
        
        self.session.commit()
        self.session.refresh(model)
        return self._payment_to_domain(model)

    # -- 领域模型转换 -------------------------------------------------------- #
    def _to_domain(self, model: OrderModel) -> Order:
        """数据库模型 → 领域模型。"""
        mode = Mode(
            account_source=AccountSource(model.account_source),
            card_source=CardSource(model.card_source),
            billing_mode=BillingMode(model.billing_mode),
            risk_acknowledged=model.risk_acknowledged,
        )
        return Order(
            id=model.id,
            idempotency_key=model.idempotency_key or "",
            plan=Plan(model.plan),
            billing_country=model.billing_country,
            mode=mode,
            status=OrderStatus(model.status),
            amount=model.amount,
            cost=model.cost,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def _job_to_domain(self, model: JobModel) -> Job:
        """任务模型 → 领域模型。"""
        return Job(
            id=model.id,
            order_id=model.order_id,
            stage=model.stage,
            attempts=model.attempts,
            locked_by=model.locked_by,
            locked_at=model.locked_at,
            last_error=model.last_error,
            checkout_url=model.checkout_url,
        )

    def _payment_to_domain(self, model: PaymentAttemptModel) -> PaymentAttempt:
        """支付尝试模型 → 领域模型。"""
        return PaymentAttempt(
            id=model.id,
            order_id=model.order_id,
            provider=model.provider,
            status=PaymentStatus(model.status),
            next_action=model.next_action,
            proxy_role=model.proxy_role,
        )


def get_repository(session: Optional[Session] = None) -> OrderRepository:
    """获取存储实例（依赖注入辅助函数）。"""
    if session is None:
        db = get_db()
        session = next(db.get_session())
    return OrderRepository(session)


__all__ = ["OrderRepository", "get_repository"]
