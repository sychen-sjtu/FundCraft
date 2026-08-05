from __future__ import annotations

from dataclasses import dataclass
from getpass import getpass
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback is not expected here
    import tomli as tomllib  # type: ignore[no-redef]

from src.security.secret_crypto import decrypt_text, is_encrypted_value


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    key: str


def _resolve_secret_password(url_value: str, key_value: str, secret_password: str | None) -> str:
    if secret_password:
        return secret_password

    env_password = os.environ.get("FUNDCRAFT_SECRET_PASSPHRASE", "").strip()
    if env_password:
        return env_password

    if url_value.strip() or key_value.strip():
        return getpass("请输入 secrets.toml 中加密字段使用的解密口令：")

    return ""


def _read_streamlit_secrets(project_root: Path) -> dict[str, Any]:
    secrets_path = project_root / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        return {}

    with secrets_path.open("rb") as file_obj:
        return tomllib.load(file_obj)


def load_access_password(project_root: Path | None = None) -> str:
    passwords = load_access_passwords(project_root)
    return passwords[0] if passwords else ""


def load_access_passwords(project_root: Path | None = None) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    security = secrets.get("security", {}) if isinstance(secrets, dict) else {}
    if not isinstance(security, dict):
        return []

    raw_passwords = security.get("access_password", [])
    if isinstance(raw_passwords, str):
        raw_passwords = [raw_passwords]

    if not isinstance(raw_passwords, list):
        return []

    passwords: list[str] = []
    for password in raw_passwords:
        normalized = str(password).strip()
        if normalized:
            passwords.append(normalized)

    return passwords


def load_fund_codes(project_root: Path | None = None) -> list[str]:
    """Load the list of fund codes from the TOML funds section."""
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    funds = secrets.get("funds", {}) if isinstance(secrets, dict) else {}
    if not isinstance(funds, dict):
        return []

    raw_codes = funds.get("fund_codes", [])
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]

    if not isinstance(raw_codes, list):
        return []

    return [str(code).strip() for code in raw_codes if str(code).strip()]


def load_supabase_settings(project_root: Path | None = None, *, secret_password: str | None = None) -> SupabaseSettings:
    """Load Supabase settings from Streamlit secrets or environment-style TOML files."""
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    supabase = secrets.get("supabase", {}) if isinstance(secrets, dict) else {}

    raw_url = str(supabase.get("url", "")).strip() if isinstance(supabase, dict) else ""
    raw_key = str(supabase.get("key", "")).strip() if isinstance(supabase, dict) else ""
    resolved_password = _resolve_secret_password(raw_url, raw_key, secret_password)

    def _resolve_value(raw_value: str) -> str:
        if not raw_value:
            return ""

        encrypted = is_encrypted_value(raw_value)
        if not encrypted:
            return raw_value

        if not resolved_password:
            raise ValueError("加密字段需要解密口令。")

        return decrypt_text(raw_value, resolved_password)

    url = _resolve_value(raw_url)
    key = _resolve_value(raw_key)
    return SupabaseSettings(url=url, key=key)


def supabase_settings_ready(settings: SupabaseSettings) -> bool:
    return bool(settings.url and settings.key)