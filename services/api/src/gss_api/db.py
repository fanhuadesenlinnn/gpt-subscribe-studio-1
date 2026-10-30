"""SQLite 持久化层（SQLAlchemy 2.x）。

实现 tasks.md T010–T011：
- 订单、任务、支付尝试的持久化模型
- 幂等键唯一约束
- 数据库初始化与迁移基础

宪法第 I 条：**不得**存储 session / token / 卡号 / CVV / 代理明文口令。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    DECIMAL,
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from gss_contracts import OrderStatus, PaymentStatus, Plan


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """所有模型的基类。"""

    pass


class OrderModel(Base):
    """订单持久化模型。"""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    plan: Mapped[str] = mapped_column(Enum(Plan), nullable=False)
    billing_country: Mapped[str] = mapped_column(String(2), nullable=False)
    
    # 模式开关（三个正交维度）
    account_source: Mapped[str] = mapped_column(String(32), nullable=False)
    card_source: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_acknowledged: Mapped[bool] = mapped_column(nullable=False, default=False)
    
    status: Mapped[str] = mapped_column(
        Enum(OrderStatus), nullable=False, default=OrderStatus.CREATED, index=True
    )
    
    # 金额与成本（可为空，支付后填充）
    amount: Mapped[Optional[Decimal]] = mapped_column(
        DECIMAL(10, 2), nullable=True
    )
    cost: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(10, 2), nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_idempotency_key"),
        CheckConstraint(
            "billing_mode IN ('standard', 'promo')", name="ck_billing_mode"
        ),
    )


class JobModel(Base):
    """编排任务模型。支持断点续跑与占锁。"""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    
    # 分布式锁字段
    locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    
    # 已脱敏的错误信息
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    
    # 结账链接（阶段一产出）
    checkout_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class PaymentAttemptModel(Base):
    """支付尝试记录。"""

    __tablename__ = "payment_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="direct_card"
    )
    status: Mapped[str] = mapped_column(
        Enum(PaymentStatus), nullable=False, default=PaymentStatus.INITIATED
    )
    
    # next_action 为不透明 JSON（如 3DS 重定向数据）
    next_action: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # 使用的代理角色（提链 / 支付阶段隔离）
    proxy_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class Database:
    """数据库连接与会话管理。"""

    def __init__(self, url: str = "sqlite:///data/gss.db") -> None:
        self.engine = create_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

    def create_tables(self) -> None:
        """创建所有表（初始化用，非迁移）。"""
        Base.metadata.create_all(bind=self.engine)

    def get_session(self):
        """获取数据库会话（依赖注入用）。"""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()


# 全局数据库实例（可通过环境变量覆盖）
_db: Optional[Database] = None


def init_db(url: str = "sqlite:///data/gss.db") -> Database:
    """初始化全局数据库实例。"""
    global _db
    _db = Database(url)
    _db.create_tables()
    return _db


def get_db() -> Database:
    """获取全局数据库实例。"""
    if _db is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _db


__all__ = [
    "Base",
    "OrderModel",
    "JobModel",
    "PaymentAttemptModel",
    "Database",
    "init_db",
    "get_db",
]
