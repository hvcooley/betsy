from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    env: str = "dev"
    anthropic_api_key: str = ""
    database_url: str = "sqlite:///./betsy.db"


settings = Settings()
