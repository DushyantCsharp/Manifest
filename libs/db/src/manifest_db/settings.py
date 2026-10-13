from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MANIFEST_DB_", env_file=".env", extra="ignore")

    url: str = "postgresql+asyncpg://manifest:manifest@localhost:5440/manifest"
