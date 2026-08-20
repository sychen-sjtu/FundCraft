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


@dataclass(frozen=True)
class IndexSpec:
    """指数注册表条目（TOML [indexes.registry] → index_master）。"""

    index_code: str
    index_name: str = ""
    index_category: str = "strategy"  # strategy / benchmark / broad
    is_total_return: bool = False
    exchange: str = ""
    source: str = "csindex"


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


def load_index_registry(project_root: Path | None = None) -> dict[str, IndexSpec]:
    """读取指数注册表（TOML [indexes.registry]），同步时写入 index_master。

    :return: {index_code: IndexSpec}
    """
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    indexes = secrets.get("indexes", {}) if isinstance(secrets, dict) else {}
    registry = indexes.get("registry", {}) if isinstance(indexes, dict) else {}

    result: dict[str, IndexSpec] = {}
    if isinstance(registry, dict):
        for code, cfg in registry.items():
            code_s = str(code).strip()
            if not code_s or not isinstance(cfg, dict):
                continue
            result[code_s] = IndexSpec(
                index_code=code_s,
                index_name=str(cfg.get("name", "")).strip(),
                index_category=str(cfg.get("category", "strategy")).strip() or "strategy",
                is_total_return=bool(cfg.get("total_return", False)),
                exchange=str(cfg.get("exchange", "")).strip(),
                source=str(cfg.get("source", "csindex")).strip() or "csindex",
            )
    return result


def load_fund_tracking_index(project_root: Path | None = None) -> list[tuple[str, str, str]]:
    """读取基金→指数映射配置（[funds.categories.*].index_codes）。

    返回 [(fund_code, index_code, role)]；当前 index_codes 均为策略底层指数，role='strategy'。
    后续如需区分基准指数，可在类别配置里扩展 role。
    """
    root = project_root or Path(__file__).resolve().parents[1]
    categories = load_fund_categories(root)
    rows: list[tuple[str, str, str]] = []
    for category in categories.values():
        for fund_code, index_code in category.index_codes.items():
            rows.append((fund_code, index_code, "strategy"))
    return rows


def load_fund_codes(project_root: Path | None = None) -> list[str]:
    """Load the list of fund codes across all categories (or from legacy [funds] fund_codes)."""
    categories = load_fund_categories(project_root)
    codes: list[str] = []
    for category in categories.values():
        for code in category.fund_codes:
            if code not in codes:
                codes.append(code)
    return codes


def load_market_index_codes(project_root: Path | None = None) -> list[str]:
    """读取我的基金页顶部市场指数条展示的指数代码（TOML [ui.market_indexes].codes）。

    顺序即展示顺序；缺省回退 ["000001", "000300"]（上证指数、沪深300）。
    """
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    ui = secrets.get("ui", {}) if isinstance(secrets, dict) else {}
    codes = ui.get("market_indexes", {}).get("codes", []) if isinstance(ui, dict) else []
    if isinstance(codes, str):
        codes = [codes]
    if isinstance(codes, list) and codes:
        return [str(code).strip() for code in codes if str(code).strip()]
    return ["000001", "000300"]


def load_compare_index_codes(project_root: Path | None = None) -> list[str]:
    """读取业绩走势可选对比指数（TOML [ui.compare_indexes].codes）。

    顺序即下拉框选项顺序；缺省回退 ["000300S"]（沪深300全收益）。
    """
    root = project_root or Path(__file__).resolve().parents[1]
    secrets = _read_streamlit_secrets(root)
    ui = secrets.get("ui", {}) if isinstance(secrets, dict) else {}
    codes = ui.get("compare_indexes", {}).get("codes", []) if isinstance(ui, dict) else []
    if isinstance(codes, str):
        codes = [codes]
    if isinstance(codes, list) and codes:
        return [str(code).strip() for code in codes if str(code).strip()]
    return ["000300S"]


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


# 需要显示「国债期货加仓信号」的面板类型（债基专用，替代旧 bond_signal 配置）
BOND_SIGNAL_PANELS = frozenset({"债基"})


def load_bond_signal_fund_codes(project_root: Path | None = None) -> list[str]:
    """从类别配置推导需要显示「国债期货加仓信号」的基金。

    规则：panel ∈ BOND_SIGNAL_PANELS（如「债基」）的类别下所有基金都需要。
    目前为债基 007171；新增债基只需新增类别并指定 panel = 债基。
    """
    categories = load_fund_categories(project_root)
    codes: list[str] = []
    for category in categories.values():
        if category.panel in BOND_SIGNAL_PANELS:
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

        # 惰性导入安全层：避免在模块导入期拉取 cryptography 等重依赖（云端 Python 3.14 更稳）
        from src.security.secret_crypto import decrypt_text, is_encrypted_value

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