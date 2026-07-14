"""Hashing password con scrypt (stdlib) e bootstrap dell'utente admin iniziale."""

from __future__ import annotations

import hashlib
import hmac
import os

from app.config import get_settings
from app.models import User
from app.storage import store

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, dk_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(dk_hex)),
        )
        return hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    except (ValueError, TypeError):
        return False


def authenticate(username: str, password: str) -> User | None:
    user = store.get_user(username)
    if user and verify_password(password, user.password_hash):
        return user
    return None


def ensure_bootstrap_admin() -> None:
    """Crea l'utente admin iniziale se non esiste alcun utente."""
    if store.list_users():
        return
    settings = get_settings()
    store.upsert_user(
        User(
            username=settings.bootstrap_admin_username,
            password_hash=hash_password(settings.bootstrap_admin_password),
        )
    )
