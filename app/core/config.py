from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql://admin:secret@postgres:5432/credit_ratings"
    data_dir: str = "/app/data"
    log_level: str = "INFO"


settings = Settings()
