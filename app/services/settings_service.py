"""用户设置：自动任务的开关（默认全部开启）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import UserSetting

SETTING_LABELS = {
    "auto_daily_brief": "自动盘后简报（交易日 16:35）",
    "auto_weekly": "自动周报（周六 10:00）",
    "auto_monthly": "自动月报（每月 1 日 10:00）",
}
DEFAULT_ON = {"auto_daily_brief", "auto_weekly", "auto_monthly"}


async def get_setting(session: AsyncSession, user_id: int, key: str) -> bool:
    row = (
        await session.execute(
            select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.setting_key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        return key in DEFAULT_ON
    return row.setting_value == "1"


async def set_setting(session: AsyncSession, user_id: int, key: str, value: bool) -> None:
    row = (
        await session.execute(
            select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.setting_key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(UserSetting(user_id=user_id, setting_key=key, setting_value="1" if value else "0"))
    else:
        row.setting_value = "1" if value else "0"
    await session.flush()


async def all_settings(session: AsyncSession, user_id: int) -> dict[str, bool]:
    return {k: await get_setting(session, user_id, k) for k in SETTING_LABELS}
