import enum
import unicodedata
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 锚定到 backend/ 根，避免 env_file 随进程 CWD 漂移
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_MIN_SECRET_LENGTH = 32
_INSECURE_SECRET_MARKERS = ("change-me", "replace-me", "your-secret", "example-secret")


class DingTalkMode(enum.StrEnum):
    """Outbound DingTalk transport mode.

    Sandbox remains the default even when credentials are present.  Enabling
    live delivery is an explicit deployment decision and requires a public
    HTTPS URL for the review/appeal action card.
    """

    SANDBOX = "sandbox"
    LIVE = "live"


class Settings(BaseSettings):
    """应用配置。全部来自环境变量 / backend/.env（前缀 COMP_），禁止硬编码敏感值。

    fail-closed：database_url 与 secret_key 均为必填，未配置时启动即
    ValidationError（不变量 4/8）。本地开发：cp deploy/.env.example backend/.env。
    """

    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ROOT / ".env"),
        env_prefix="COMP_",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "compensation-platform"
    debug: bool = False
    database_url: str

    # 认证（不变量4：无默认，缺失即 fail-closed）
    secret_key: str
    # PII 列级加密口令（不变量7）。任意高熵字符串；轮换需重新加密现有数据（S17）。
    encryption_key: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    # 登录限速（防爆破）
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    # cookie：生产必须 True（HTTPS）；本地 HTTP 开发可经 env 关掉
    cookie_secure: bool = True
    # A non-secret identifier exposed only by /api/health when an isolated E2E
    # stack opts in. Production leaves this unset, so health output is unchanged.
    e2e_target_marker: str | None = None

    # DingTalk enterprise-internal application.  The client secret is always a
    # SecretStr so accidental Settings repr/logging cannot disclose it.  Merely
    # configuring credentials does not enable outbound salary notifications.
    dingtalk_mode: DingTalkMode = DingTalkMode.SANDBOX
    dingtalk_app_id: str | None = None
    # Public CorpId used by the H5 JSAPI when requesting a one-time login code.
    dingtalk_corp_id: str | None = None
    dingtalk_client_id: str | None = None
    dingtalk_client_secret: SecretStr | None = None
    dingtalk_agent_id: int | None = None
    dingtalk_public_base_url: AnyHttpUrl | None = None
    dingtalk_timeout_seconds: float = 5.0
    dingtalk_review_session_ttl_minutes: int = 15
    dingtalk_review_session_max_attempts: int = 10
    dingtalk_review_session_ip_max_attempts: int = 300
    dingtalk_review_session_attempt_window_minutes: int = 15
    dingtalk_review_link_ttl_hours: int = 168
    # Contact/attendance reads are a separate, explicit capability.  Keeping
    # outbound transport in sandbox does not disable these reads once an
    # administrator deliberately enables them.
    dingtalk_read_sync_enabled: bool = False
    # Comma-separated top-level DingTalk departments whose descendant `*店`
    # nodes may be created as new local stores after an HR preview.
    dingtalk_store_root_names: str = "潮发运营中心,九亩地,中山"
    dingtalk_dining_manager_titles: str = "店长"
    dingtalk_kitchen_manager_titles: str = "厨房经理"
    dingtalk_org_sync_freshness_minutes: int = 5
    dingtalk_org_root_mappings: str = ""
    dingtalk_org_sync_timezone: str = "Asia/Shanghai"

    @field_validator("secret_key", "encryption_key")
    @classmethod
    def reject_insecure_secret_placeholders(cls, value: str) -> str:
        """Refuse copied example values before the API accepts any request.

        A non-empty placeholder is worse than a missing value: it lets a
        seemingly healthy deployment sign forgeable tokens and encrypt PII with
        a publicly known key.  This check deliberately validates only obvious
        unsafe cases; entropy still belongs to the deployment secret manager.
        """
        normalized = value.strip()
        if len(normalized) < _MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {_MIN_SECRET_LENGTH} characters long")
        if any(marker in normalized.lower() for marker in _INSECURE_SECRET_MARKERS):
            raise ValueError("must not use an example or placeholder secret")
        return normalized

    @field_validator("database_url")
    @classmethod
    def reject_placeholder_database_url(cls, value: str) -> str:
        if "change-me" in value.lower():
            raise ValueError("must not use a placeholder database credential")
        return value

    @field_validator("e2e_target_marker", mode="before")
    @classmethod
    def normalize_optional_e2e_target_marker(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("dingtalk_app_id", "dingtalk_corp_id", "dingtalk_client_id", mode="before")
    @classmethod
    def normalize_optional_dingtalk_identifiers(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("dingtalk_client_secret", mode="before")
    @classmethod
    def normalize_optional_dingtalk_secret(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("dingtalk_agent_id")
    @classmethod
    def validate_dingtalk_agent_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("dingtalk_timeout_seconds")
    @classmethod
    def validate_dingtalk_timeout(cls, value: float) -> float:
        if not 1.0 <= value <= 30.0:
            raise ValueError("must be between 1 and 30 seconds")
        return value

    @field_validator("dingtalk_review_session_ttl_minutes")
    @classmethod
    def validate_dingtalk_review_session_ttl(cls, value: int) -> int:
        if not 5 <= value <= 30:
            raise ValueError("must be between 5 and 30 minutes")
        return value

    @field_validator("dingtalk_review_session_max_attempts")
    @classmethod
    def validate_dingtalk_review_session_max_attempts(cls, value: int) -> int:
        if not 3 <= value <= 100:
            raise ValueError("must be between 3 and 100 attempts")
        return value

    @field_validator("dingtalk_review_session_ip_max_attempts")
    @classmethod
    def validate_dingtalk_review_session_ip_max_attempts(cls, value: int) -> int:
        if not 10 <= value <= 5000:
            raise ValueError("must be between 10 and 5000 attempts")
        return value

    @field_validator("dingtalk_review_session_attempt_window_minutes")
    @classmethod
    def validate_dingtalk_review_session_attempt_window(cls, value: int) -> int:
        if not 1 <= value <= 60:
            raise ValueError("must be between 1 and 60 minutes")
        return value

    @field_validator("dingtalk_review_link_ttl_hours")
    @classmethod
    def validate_dingtalk_review_link_ttl(cls, value: int) -> int:
        if not 1 <= value <= 720:
            raise ValueError("must be between 1 and 720 hours")
        return value

    @field_validator("dingtalk_store_root_names")
    @classmethod
    def validate_dingtalk_store_root_names(cls, value: str) -> str:
        names = [
            " ".join(unicodedata.normalize("NFKC", name).split())
            for name in value.split(",")
            if name.strip()
        ]
        if not names or len(names) > 20 or any(len(name) > 128 for name in names):
            raise ValueError("must contain between 1 and 20 valid department names")
        return ",".join(dict.fromkeys(names))

    @field_validator("dingtalk_dining_manager_titles", "dingtalk_kitchen_manager_titles")
    @classmethod
    def validate_dingtalk_manager_titles(cls, value: str) -> str:
        titles = [
            " ".join(unicodedata.normalize("NFKC", title).split())
            for title in value.split(",")
            if title.strip()
        ]
        if not titles or len(titles) > 20 or any(len(title) > 64 for title in titles):
            raise ValueError("must contain between 1 and 20 valid manager titles")
        return ",".join(dict.fromkeys(titles))

    @field_validator("dingtalk_org_sync_freshness_minutes")
    @classmethod
    def validate_dingtalk_org_sync_freshness(cls, value: int) -> int:
        if not 1 <= value <= 15:
            raise ValueError("must be between 1 and 15 minutes")
        return value

    @field_validator("dingtalk_org_root_mappings")
    @classmethod
    def validate_dingtalk_org_root_mappings(cls, value: str) -> str:
        pairs: list[tuple[int, str]] = []
        for raw_pair in value.split(","):
            if not raw_pair.strip():
                continue
            raw_id, separator, raw_code = raw_pair.partition(":")
            code = raw_code.strip()
            if separator != ":" or not raw_id.strip().isdigit() or not code:
                raise ValueError("root mappings must use <positive-id>:<local-code>")
            remote_id = int(raw_id.strip())
            if remote_id <= 0 or len(code) > 64 or ":" in code or "," in code:
                raise ValueError("root mappings contain an invalid id or local code")
            pairs.append((remote_id, code))
        if len(pairs) > 20:
            raise ValueError("root mappings must contain at most 20 roots")
        if len({remote_id for remote_id, _ in pairs}) != len(pairs):
            raise ValueError("root mappings contain a duplicate remote root")
        return ",".join(f"{remote_id}:{code}" for remote_id, code in pairs)

    @field_validator("dingtalk_org_sync_timezone")
    @classmethod
    def validate_dingtalk_org_sync_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("must be a valid IANA timezone") from exc
        return normalized

    @model_validator(mode="after")
    def validate_dingtalk_configuration(self) -> "Settings":
        credential_parts = (
            self.dingtalk_client_id is not None,
            self.dingtalk_client_secret is not None,
            self.dingtalk_agent_id is not None,
        )
        if any(credential_parts) and not all(credential_parts):
            raise ValueError(
                "DingTalk credentials must include client_id, client_secret, and agent_id"
            )
        if self.dingtalk_client_secret is not None:
            secret = self.dingtalk_client_secret.get_secret_value()
            if len(secret) < 16 or any(
                marker in secret.lower() for marker in _INSECURE_SECRET_MARKERS
            ):
                raise ValueError("DingTalk client_secret must not be short or a placeholder")
        if self.dingtalk_mode is DingTalkMode.LIVE:
            if not all(credential_parts):
                raise ValueError("DingTalk live mode requires complete application credentials")
            if self.dingtalk_corp_id is None:
                raise ValueError("DingTalk live mode requires dingtalk_corp_id for H5 review")
            if self.dingtalk_public_base_url is None:
                raise ValueError("DingTalk live mode requires dingtalk_public_base_url")
            if self.dingtalk_public_base_url.scheme != "https":
                raise ValueError("DingTalk live mode requires an HTTPS public base URL")
            if not self.dingtalk_read_sync_enabled:
                raise ValueError("DingTalk live mode requires read sync for manager authorization")
        if self.dingtalk_read_sync_enabled and not all(credential_parts):
            raise ValueError("DingTalk read sync requires complete application credentials")
        if self.dingtalk_read_sync_enabled and self.dingtalk_corp_id is None:
            raise ValueError("DingTalk read sync requires dingtalk_corp_id")
        if (
            self.dingtalk_mode is DingTalkMode.LIVE
            and self.dingtalk_read_sync_enabled
            and not self.dingtalk_org_root_mapping_pairs
        ):
            raise ValueError("DingTalk live read sync requires root mappings")
        dining_titles = {title.casefold() for title in self.dingtalk_dining_manager_title_set}
        kitchen_titles = {title.casefold() for title in self.dingtalk_kitchen_manager_title_set}
        if dining_titles & kitchen_titles:
            raise ValueError("DingTalk manager title sets must not overlap")
        if self.dingtalk_review_session_ip_max_attempts < self.dingtalk_review_session_max_attempts:
            raise ValueError("DingTalk session IP attempt limit must cover the per-review limit")
        return self

    @property
    def dingtalk_credentials_configured(self) -> bool:
        return (
            self.dingtalk_client_id is not None
            and self.dingtalk_client_secret is not None
            and self.dingtalk_agent_id is not None
        )

    @property
    def dingtalk_store_root_name_set(self) -> frozenset[str]:
        return frozenset(self.dingtalk_store_root_names.split(","))

    @property
    def dingtalk_org_root_mapping_pairs(self) -> tuple[tuple[int, str], ...]:
        if not self.dingtalk_org_root_mappings:
            return ()
        return tuple(
            (int(raw_id), code)
            for pair in self.dingtalk_org_root_mappings.split(",")
            for raw_id, code in [pair.split(":", 1)]
        )

    @property
    def dingtalk_dining_manager_title_set(self) -> frozenset[str]:
        return frozenset(self.dingtalk_dining_manager_titles.split(","))

    @property
    def dingtalk_kitchen_manager_title_set(self) -> frozenset[str]:
        return frozenset(self.dingtalk_kitchen_manager_titles.split(","))


@lru_cache
def get_settings() -> Settings:
    # 必填字段由 pydantic-settings 在运行时从环境/.env 注入
    return Settings()  # type: ignore[call-arg]
