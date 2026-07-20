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
    # REVO Identity (JWKS). When set, UI + /api/upload require IdP JWT.
    identity_issuer_url: str = ""
    identity_client_id: str = "revo-manifests"


settings = Settings()
