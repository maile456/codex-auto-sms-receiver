from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from .hero_sms import install_hero_sms_patch
from .settings import Settings
from .totp_auth import current_totp
from .upstream_location import resolve_upstream_root


SAFE_DEFAULTS = {
    "OUTLOOK_FETCH_MODE": "direct",
    "CODEX_OAUTH_DRIVER": "protocol",
    "CODEX_AUTH_URL_SOURCE": "local",
    "PROXY_POOL": "",
    "USE_EMAIL_SERVICE": "True",
}

GENERIC_API_OTP_MAX_WAIT_SECONDS = 90
GENERIC_API_OTP_POLL_INTERVAL_SECONDS = 3
GENERIC_API_OTP_ATTEMPTS = 1


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _load_runtime_environment(settings: Settings) -> None:
    for key, value in SAFE_DEFAULTS.items():
        os.environ.setdefault(key, value)
    env_path = settings.project_root / ".env"
    if env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if key and value is not None:
                os.environ[str(key)] = str(value)
    # Hero SMS is the only supported provider in this login-only project.
    # Force the protocol/login-only selectors after loading .env so stale
    # settings or inherited variables cannot route a job to an unbundled
    # browser or registration-oriented driver.
    os.environ["CODEX_OAUTH_DRIVER"] = "protocol"
    os.environ["SMS_PROVIDER"] = "hero"
    os.environ["SMS_SERVICE"] = "dr"
    os.environ.pop("SMS_PROVIDER_ORDER", None)


def _ensure_upstream_imports(settings: Settings):
    upstream_root = resolve_upstream_root(settings.project_root)
    if not (upstream_root / "core" / "codex_oauth.py").is_file():
        raise RuntimeError(f"未找到原项目 Codex OAuth 模块: {upstream_root}")
    root_text = str(upstream_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    _load_runtime_environment(settings)
    import config as upstream_config

    upstream_config.reload_all()
    from core import codex_oauth

    # 原模块只用该根目录决定 Codex 凭证/回执的保存位置。
    # 重定向到当前 login-only 项目的 data/，不污染原项目数据。
    codex_oauth._PROJECT_ROOT = settings.data_dir
    return codex_oauth


def _outlook_otp_provider(mailbox: dict) -> tuple[Callable, Callable[[], None]]:
    from core import outlook_client

    email = str(mailbox["email"])
    account = outlook_client.OutlookAccount(
        email=email,
        password=str(mailbox.get("password") or ""),
        client_id=str(mailbox.get("client_id") or ""),
        refresh_token=str(mailbox.get("refresh_token") or ""),
    )
    outlook_client._CONTEXT_CACHE[email] = account

    def get_otp(target_email: str, after_ts: float, **kwargs) -> str:
        if target_email.casefold() != email.casefold():
            raise RuntimeError("OTP 请求邮箱与已导入账号不一致")
        return outlook_client.fetch_latest_otp(target_email, after_ts=after_ts, **kwargs)

    def cleanup() -> None:
        outlook_client._CONTEXT_CACHE.pop(email, None)

    return get_otp, cleanup


def _generic_api_otp_provider(mailbox: dict) -> tuple[Callable, Callable[[], None]]:
    from core import generic_api_mail_client

    email = str(mailbox["email"])
    account = generic_api_mail_client.GenericApiEmailAccount(
        email=email,
        code_url=str(mailbox.get("code_url") or ""),
    )
    generic_api_mail_client._CONTEXT_CACHE[email] = account

    def get_otp(target_email: str, after_ts: float, **kwargs) -> str:
        if target_email.casefold() != email.casefold():
            raise RuntimeError("OTP 请求邮箱与已导入账号不一致")
        # 取码页的邮件到达常比 30 秒更慢。使用独立可配置窗口，
        # 避免全局 OTP 参数较短时过早判定失败。
        kwargs["max_wait"] = _bounded_env_int(
            "GENERIC_API_OTP_MAX_WAIT",
            GENERIC_API_OTP_MAX_WAIT_SECONDS,
            30,
            300,
        )
        kwargs.setdefault(
            "poll_interval",
            _bounded_env_int(
                "GENERIC_API_OTP_POLL_INTERVAL",
                GENERIC_API_OTP_POLL_INTERVAL_SECONDS,
                1,
                30,
            ),
        )
        return generic_api_mail_client.fetch_latest_otp(target_email, after_ts=after_ts, **kwargs)

    # 每轮都会等待上方 max_wait；允许在 WebUI 中配置是否重发邮箱并再次取码。
    get_otp.codex_max_email_otp_attempts = _bounded_env_int(
        "GENERIC_API_OTP_ATTEMPTS",
        GENERIC_API_OTP_ATTEMPTS,
        1,
        5,
    )

    def cleanup() -> None:
        generic_api_mail_client._CONTEXT_CACHE.pop(email, None)

    return get_otp, cleanup


def run_codex_only(settings: Settings, mailbox: dict) -> dict:
    """Run only the upstream existing-account Codex OAuth entrypoint."""

    email = str(mailbox.get("email") or "").strip()
    if not email:
        raise ValueError("邮箱为空")
    source = str(mailbox.get("source") or "").strip().lower()
    codex_oauth = _ensure_upstream_imports(settings)

    password = ""
    totp_provider = None
    if source == "outlook":
        otp_provider, cleanup = _outlook_otp_provider(mailbox)
    elif source in {"generic_api", "code_url"}:
        otp_provider, cleanup = _generic_api_otp_provider(mailbox)
    elif source == "password_totp":
        password = str(mailbox.get("password") or "")
        totp_secret = str(mailbox.get("totp_secret") or "")
        if not password or not totp_secret:
            raise ValueError("密码 + 2FA 账号缺少密码或 TOTP 密钥")
        otp_provider = None
        totp_provider = lambda: current_totp(totp_secret)
        cleanup = lambda: None
    else:
        raise ValueError(f"暂不支持的邮箱来源: {source}")

    sms_provider = getattr(codex_oauth, "sms_provider", None)
    hero_patch = None
    try:
        if sms_provider is None:
            raise RuntimeError("原项目未提供短信生命周期模块，无法启用 Hero SMS")
        hero_patch = install_hero_sms_patch(sms_provider)
        if hero_patch is None:
            raise RuntimeError("Hero SMS 适配器安装失败，已阻止使用其他接码平台")
        # 这是原项目的 Codex 补跑入口；不导入也不调用 main.run_registration。
        kwargs = {
            "otp_provider": otp_provider,
            "proxy": None,
            "force": True,
        }
        if source == "password_totp":
            kwargs.update(password=password, totp_provider=totp_provider)
        result = codex_oauth.run_codex_oauth(email, **kwargs)
    finally:
        try:
            if hero_patch is not None:
                hero_patch.restore()
        finally:
            cleanup()

    if not isinstance(result, dict):
        raise RuntimeError("原项目 Codex OAuth 未返回结构化结果")
    return result
