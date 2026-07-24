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

    # URL pubblico di questa installazione (es. https://servermcp.cascinanet.it), usato per
    # costruire redirect URI OAuth (es. callback LinkedIn) che devono corrispondere esattamente
    # a quanto registrato lato provider. Vuoto = derivato dalla richiesta in arrivo (fallback
    # ragionevole in locale, ma dietro un proxy TLS conviene impostarlo esplicitamente).
    public_base_url: str = ""

    @property
    def servers_file(self) -> Path:
        return self.data_dir / "servers.json"

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def sqlite_dir(self) -> Path:
        """Cartella per i database dei server MCP di tipo 'sqlite': sotto data_dir così vive
        sempre sul disco/volume persistente, qualunque sia il deployment (Lightsail, Azure,
        Docker)."""
        return self.data_dir / "db"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.sqlite_dir.mkdir(parents=True, exist_ok=True)
    return settings
