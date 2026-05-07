from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    apify_token: str = ""
    hunter_api_key: str = ""
    exa_api_key: str = ""
    apollo_api_key: str = ""
    budget_usd: float = 100.0

    project_root: Path = Path(__file__).resolve().parents[2]

    @property
    def cache_dir(self) -> Path:
        return self.project_root / "data" / "cache"

    @property
    def output_dir(self) -> Path:
        return self.project_root / "data" / "output"


settings = Settings()
