from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/revo.db"
    app_title: str = "REVO Manifest Ingest"
    # 0 = no size limit (process every sheet in every uploaded workbook)
    max_upload_mb: int = 0
    # Protect /api/v1/* when set. Empty = open (local only).
    api_key: str = ""
    # Comma-separated origins for external reports, or * 
    cors_origins: str = "*"


settings = Settings()
