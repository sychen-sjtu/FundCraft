from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class FundCategory:
    """基金类别：决定 UI 标签页、每只基金的展示面板，以及对应指数。"""

    name: str
    fund_codes: tuple[str, ...]
    panel: str = "净值"
    index_codes: dict[str, str] = field(default_factory=dict)  # 基金代码 -> 指数代码


# 未配置 panel 时的默认面板
DEFAULT_PANEL = "净值"


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


def load_fund_categories(project_root: Path | None = None) -> dict[str, FundCategory]:
    """Load fund categories from the TOML [funds.categories] section.

    结构示例：
        [funds.categories."红利低波"]
        fund_codes = ["008163"]
        panel = "红利低波"          # 决定该类别基金的展示面板（缺省为「净值」）

    :return: {类别名: FundCategory}
    """
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    funds = secrets.get("funds", {}) if isinstance(secrets, dict) else {}
    if not isinstance(funds, dict):
        return {}

    categories = funds.get("categories", {})
    if isinstance(categories, dict) and categories:
        result: dict[str, FundCategory] = {}
        for name, config in categories.items():
            if not isinstance(config, dict):
                continue
            raw_codes = config.get("fund_codes", [])
            if isinstance(raw_codes, str):
                raw_codes = [raw_codes]
            if not isinstance(raw_codes, list):
                continue
            codes = [str(code).strip() for code in raw_codes if str(code).strip()]
            if not codes:
                continue
            panel = str(config.get("panel", "")).strip() or DEFAULT_PANEL

            # 基金代码 -> 对应指数代码（用于拉取指数股息率）
            index_codes: dict[str, str] = {}
            raw_index_codes = config.get("index_codes", {})
            if isinstance(raw_index_codes, dict):
                for fund_code, index_code in raw_index_codes.items():
                    fund_code_s = str(fund_code).strip()
                    index_code_s = str(index_code).strip()
                    if fund_code_s and index_code_s:
                        index_codes[fund_code_s] = index_code_s

            result[str(name)] = FundCategory(
                name=str(name),
                fund_codes=tuple(codes),
                panel=panel,
                index_codes=index_codes,
            )
        return result

    # 兼容旧配置：[funds] fund_codes = [...]（归入「全部基金」，默认面板）
    raw_codes = funds.get("fund_codes", [])
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]
    if isinstance(raw_codes, list) and raw_codes:
        codes = tuple(str(code).strip() for code in raw_codes if str(code).strip())
        return {"全部基金": FundCategory(name="全部基金", fund_codes=codes, panel=DEFAULT_PANEL)}

    return {}


def load_fund_index_codes(project_root: Path | None = None) -> dict[str, str]:
    """跨类别汇总「基金代码 -> 对应指数代码」的映射。"""
    categories = load_fund_categories(project_root)
    mapping: dict[str, str] = {}
    for category in categories.values():
        for fund_code, index_code in category.index_codes.items():
            mapping[fund_code] = index_code
    return mapping


def load_fund_codes(project_root: Path | None = None) -> list[str]:
    """Load the list of fund codes across all categories (or from legacy [funds] fund_codes)."""
    categories = load_fund_categories(project_root)
    codes: list[str] = []
    for category in categories.values():
        for code in category.fund_codes:
            if code not in codes:
                codes.append(code)
    return codes


# 需要计算并入库策略因子的面板类型（取代旧的 [strategy] fund_codes 配置）
FACTOR_PANELS = frozenset({"红利低波"})


def load_factor_fund_codes(project_root: Path | None = None) -> list[str]:
    """从类别配置推导需要计算策略因子的基金。

    规则：panel ∈ FACTOR_PANELS（如「红利低波」）的类别下所有基金都需要计算因子。
    这样不需要单独的 [strategy] 配置，新增策略类别只需在类别配置里指定 panel。
    """
    categories = load_fund_categories(project_root)
    codes: list[str] = []
    for category in categories.values():
        if category.panel in FACTOR_PANELS:
            for code in category.fund_codes:
                if code not in codes:
                    codes.append(code)
    return codes


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