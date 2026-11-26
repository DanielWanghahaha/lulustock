"""用户绑定与默认组合/策略初始化。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import PolicyVersion, Portfolio, User

DEFAULT_POLICY_V1: dict = {
    "policy_version": 1,
    "status": "proposed",
    "market": "CN_A",
    "currency": "CNY",
    "planned_capital_cny": [500000, 1000000],
    "annual_return_goal": [0.05, 0.08],
    "max_drawdown_tolerance": 0.20,
    "horizon_years": [1, 3],
    "allocation_reference": {
        "core_dividend_and_broad_etf": 0.40,
        "quality_value": 0.30,
        "cyclical": 0.10,
        "cash_and_low_volatility": 0.20,
    },
    "risk_limits": {
        "initial_single_stock_weight": [0.02, 0.03],
        "max_single_stock_weight": 0.08,
        "max_industry_weight": 0.25,
        "max_cyclical_weight": 0.15,
        "min_liquid_reserve_weight": 0.15,
        "allow_leverage": False,
        "require_human_confirmation": True,
        "auto_trade": False,
    },
    "alerts": {"drawdown_thresholds": [0.05, 0.10, 0.15, 0.20], "timezone": "Asia/Shanghai"},
}


async def ensure_user(session: AsyncSession, telegram_user_id: int, display_name: str | None) -> User:
    result = await session.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_user_id=telegram_user_id, display_name=display_name)
        session.add(user)
        await session.flush()
    elif display_name and user.display_name != display_name:
        user.display_name = display_name
    return user


async def get_default_portfolio(session: AsyncSession, user_id: int) -> Portfolio:
    result = await session.execute(
        select(Portfolio).where(Portfolio.user_id == user_id, Portfolio.is_default.is_(True))
    )
    portfolio = result.scalar_one_or_none()
    if portfolio is None:
        portfolio = Portfolio(user_id=user_id, name="默认投资组合", currency="CNY", is_default=True)
        session.add(portfolio)
        await session.flush()
    return portfolio


async def ensure_policy_v1(session: AsyncSession, user_id: int) -> PolicyVersion | None:
    result = await session.execute(
        select(PolicyVersion).where(PolicyVersion.user_id == user_id, PolicyVersion.version == 1)
    )
    policy = result.scalar_one_or_none()
    if policy is None:
        policy = PolicyVersion(user_id=user_id, version=1, status="proposed", payload=DEFAULT_POLICY_V1)
        session.add(policy)
        return policy
    return None
