"""MySQL 异步引擎与会话工厂。"""
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def create_db_engine() -> AsyncEngine:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL 未配置")
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        isolation_level="REPEATABLE READ",
    )


engine = create_db_engine()
Session = async_sessionmaker(engine, expire_on_commit=False)
