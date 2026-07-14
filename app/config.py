"""Configurazione applicativa caricata da variabili d'ambiente / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    secret_key: str = "dev-only-insecure-change-me"
    session_max_age: int = 8 * 60 * 60  # 8 ore
    data_dir: Path = Path("./data")

    # Avvio iniziale: se non esiste alcun utente, ne crea uno con queste credenziali.
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin"

    @property
    def servers_file(self) -> Path:
        return self.data_dir / "servers.json"

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
