"""M1 数据模型：用户、组合、策略、总账、持仓、队列、导入、待确认动作。"""
import enum
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS6 = sa.text("CURRENT_TIMESTAMP(6)")


class Base(DeclarativeBase):
    pass


class TradeDirection(str, enum.Enum):
    """总账方向：seed=初始建仓（不影响现金）。"""

    seed = "seed"
    buy = "buy"
    sell = "sell"
    deposit = "deposit"
    withdraw = "withdraw"
    dividend = "dividend"


class PendingActionType(str, enum.Enum):
    confirm_import = "confirm_import"
    confirm_trade = "confirm_trade"


class PendingStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


class JobState(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    retry_wait = "retry_wait"
    failed = "failed"
    cancelled = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="默认投资组合")
    currency: Mapped[str] = mapped_column(CHAR(3), default="CNY")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)

    __table_args__ = (sa.UniqueConstraint("user_id", "name", name="uq_portfolio_user_name"),)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        SAEnum("proposed", "active", "retired", name="policy_status"), default="proposed"
    )
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    activated_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    __table_args__ = (sa.UniqueConstraint("user_id", "version", name="uq_policy_user_version"),)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    trade_date: Mapped[sa.Date] = mapped_column(Date)
    direction: Mapped[TradeDirection] = mapped_column(
        SAEnum(TradeDirection, name="trade_direction", values_callable=lambda e: [m.value for m in e])
    )
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(CHAR(3), default="CNY")
    source: Mapped[str] = mapped_column(SAEnum("manual", "csv", "system", name="entry_source"), default="manual")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)

    __table_args__ = (
        sa.Index("idx_ledger_portfolio_date", "portfolio_id", "trade_date"),
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    updated_at: Mapped[sa.DateTime] = mapped_column(
        DATETIME(fsp=6), server_default=TS6, onupdate=TS6
    )

    __table_args__ = (sa.UniqueConstraint("portfolio_id", "symbol", name="uq_position"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[JobState] = mapped_column(SAEnum(JobState, values_callable=lambda e: [m.value for m in e]), default=JobState.queued)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    updated_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6, onupdate=TS6)

    __table_args__ = (
        sa.Index("idx_jobs_claim", "state", "run_after", "id"),
        sa.Index("idx_jobs_lease", "state", "lease_until"),
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        SAEnum("draft", "confirmed", "cancelled", name="import_status"), default="draft"
    )
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    confirmed_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class ImportItem(Base):
    __tablename__ = "import_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cost_per_share: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    currency: Mapped[str] = mapped_column(CHAR(3))
    as_of: Mapped[sa.Date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(SAEnum("ok", "error", name="item_status"), default="ok")
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PendingAction(Base):
    """写操作待确认令牌：回调仅携带 token，服务端绑定用户与过期时间。"""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(12), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    action_type: Mapped[PendingActionType] = mapped_column(
        SAEnum(PendingActionType, values_callable=lambda e: [m.value for m in e])
    )
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[PendingStatus] = mapped_column(
        SAEnum(PendingStatus, values_callable=lambda e: [m.value for m in e]),
        default=PendingStatus.pending,
    )
    expires_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    confirmed_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class DailyQuote(Base):
    """日线收盘价（未复权），按来源留存与质量标记。"""

    __tablename__ = "daily_quotes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    trade_date: Mapped[sa.Date] = mapped_column(Date)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(CHAR(3), default="CNY")
    source: Mapped[str] = mapped_column(String(32), default="eastmoney")
    quality: Mapped[str] = mapped_column(SAEnum("ok", "stale", "missing", "conflict", name="quote_quality"), default="ok")
    source_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fetched_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)

    __table_args__ = (
        sa.UniqueConstraint("symbol", "trade_date", "source", name="uq_quote_symbol_date_source"),
        sa.Index("idx_quote_symbol_date", "symbol", "trade_date"),
    )


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"

    trade_date: Mapped[sa.Date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="eastmoney")
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)


class WatchlistItem(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price_alert_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    price_alert_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    added_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)

    __table_args__ = (sa.UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),)


class OutboxMessage(Base):
    """外发通知队列：先落库再投递，失败可重试。"""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    text: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(SAEnum("pending", "sent", "failed", name="outbox_status"), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    created_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    sent_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class JobRun(Base):
    """调度任务执行审计。"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(SAEnum("ok", "error", name="jobrun_status"), default="ok")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)
    finished_at: Mapped[sa.DateTime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class PortfolioSnapshot(Base):
    """每日组合净值快照：用于回撤与收益序列。"""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    trade_date: Mapped[sa.Date] = mapped_column(Date)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    market_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    nav: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=0)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)  # 是否所有持仓均有有效行情
    computed_at: Mapped[sa.DateTime] = mapped_column(DATETIME(fsp=6), server_default=TS6)

    __table_args__ = (sa.UniqueConstraint("portfolio_id", "trade_date", name="uq_snapshot"),)
