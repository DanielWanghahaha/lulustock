"""应用配置（从 .env 读取）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    app_tz: str = "Asia/Shanghai"
    log_level: str = "INFO"

    telegram_bot_token: str = ""
    # 逗号分隔的 Telegram 数字用户 ID；为空时拒绝所有人
    telegram_allowed_user_ids: str = ""

    enable_schedules: bool = False

    database_url: str = ""

    tushare_token: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        ids = {
            int(x.strip())
            for x in self.telegram_allowed_user_ids.split(",")
            if x.strip()
        }
        return frozenset(ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()
