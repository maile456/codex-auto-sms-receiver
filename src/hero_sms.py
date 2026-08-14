"""Hero-SMS protocol adapter for the upstream SMS provider module.

Hero-SMS exposes an SMS-Activate-compatible endpoint.  The upstream project
already owns the activation lifecycle, so this module only adapts transport,
normalizes legacy text/JSON responses, and installs an exception-safe temporary
patch without modifying the upstream checkout.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any, Mapping

from .hero_pricing import (
    HERO_SMS_API_BASE,
    HERO_SMS_SERVICE_CODE,
    extract_price_tiers,
    filter_price_tiers,
    merge_price_tiers,
)
from .sms_config import normalize_hero_countries


class HeroSmsError(RuntimeError):
    """Base error for direct use of :class:`HeroSmsAdapter`."""


class HeroSmsNoNumbersError(HeroSmsError):
    pass


class HeroSmsNoBalanceError(HeroSmsError):
    pass


def _first_proxy(value: Any) -> str:
    """Return the first configured proxy without logging credentials."""

    if isinstance(value, (list, tuple)):
        return next((str(item).strip() for item in value if str(item).strip()), "")
    text = str(value or "").strip()
    if not text or text == "[]":
        return ""
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            return _first_proxy(parsed)
    return next(
        (
            item.strip()
            for item in re.split(r"[\r\n,;]+", text)
            if item.strip()
        ),
        "",
    )


def _hero_proxy_url() -> str:
    explicit = _first_proxy(os.getenv("HERO_SMS_PROXY", ""))
    if explicit:
        return explicit
    return _first_proxy(os.getenv("PROXY_POOL", ""))


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value is not False and str(value).strip():
            return str(value).strip()
    return ""


def _phone_digits(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


class HeroSmsAdapter:
    """Make authenticated Hero-SMS requests and return legacy-compatible text."""

    def __init__(
        self,
        api_key: str,
        *,
        proxy_url: str | None = None,
        provider_error: type[Exception] = HeroSmsError,
        no_numbers_error: type[Exception] = HeroSmsNoNumbersError,
        no_balance_error: type[Exception] = HeroSmsNoBalanceError,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._provider_error = provider_error
        self._no_numbers_error = no_numbers_error
        self._no_balance_error = no_balance_error
        self._proxy_url = (
            _hero_proxy_url() if proxy_url is None else _first_proxy(proxy_url)
        )

    def _configure_http(self, http: Any) -> None:
        if not self._proxy_url:
            return
        try:
            current = getattr(http, "proxies", None)
            proxies = dict(current) if isinstance(current, Mapping) else {}
            proxies.update({"http": self._proxy_url, "https": self._proxy_url})
            http.proxies = proxies
        except Exception as exc:
            raise self._provider_error(
                f"Hero-SMS proxy configuration failed ({type(exc).__name__})"
            ) from exc

    def request(self, http: Any, params: Mapping[str, Any]) -> str:
        if not self._api_key:
            raise self._provider_error("Hero-SMS API key is empty")

        action = str(params.get("action") or "").strip()
        # The upstream flow calls legacy setStatus=1 after OpenAI accepts the
        # SMS send request. Hero's current OpenAPI lists 3/6/8 and does not
        # require a separate "SMS sent" transition, so treat 1 as a local
        # compatibility no-op instead of risking BAD_STATUS.
        if action == "setStatus" and str(params.get("status") or "") == "1":
            return "ACCESS_READY"
        request_params, payload, raw_text = self.query(http, params, include_request=True)
        normalized = self._normalize_success(action, payload, raw_text, request_params)
        error = self._error_from(normalized, normalized)
        if error is not None:
            self._raise_error(*error)
        return normalized

    def query(
        self,
        http: Any,
        params: Mapping[str, Any],
        *,
        include_request: bool = False,
    ) -> Any:
        """Return a checked Hero payload without ever exposing the API key."""

        if not self._api_key:
            raise self._provider_error("Hero-SMS API key is empty")
        action = str(params.get("action") or "").strip()
        request_params = {**dict(params), "api_key": self._api_key}
        if action in {
            "getNumber",
            "getNumberV2",
            "getPrices",
            "getPricesExtended",
            "getPricesForVerification",
            "getPricesVerification",
            "getTopCountriesByService",
        } and str(request_params.get("service") or "").strip().lower() in {
            "",
            "openai",
            "chatgpt",
        }:
            request_params["service"] = HERO_SMS_SERVICE_CODE
        self._configure_http(http)
        try:
            response = http.get(HERO_SMS_API_BASE, params=request_params)
        except Exception as exc:
            # HTTP clients may include the full query string (and therefore the
            # API key) in their exception text. Expose only the exception type.
            raise self._provider_error(
                f"Hero-SMS request failed ({type(exc).__name__})"
            ) from exc
        status_code = int(getattr(response, "status_code", 0) or 0)
        raw_text = str(getattr(response, "text", "") or "").strip()
        payload = self._json_payload(response, raw_text)

        error = self._error_from(payload, raw_text)
        if error is not None:
            self._raise_error(*error)
        if status_code != 200:
            if status_code == 401:
                self._raise_error("BAD_KEY", "Unauthorized")
            if status_code == 402:
                self._raise_error("NO_BALANCE", "Payment Required")
            raise self._provider_error(f"Hero-SMS HTTP {status_code or 'unknown'}")
        if include_request:
            return request_params, payload, raw_text
        return payload

    @staticmethod
    def _json_payload(response: Any, raw_text: str) -> Any:
        try:
            return response.json()
        except Exception:
            pass
        if raw_text and raw_text[:1] in {'"', "{", "["}:
            try:
                return json.loads(raw_text)
            except (TypeError, ValueError):
                pass
        return raw_text

    def _normalize_success(
        self,
        action: str,
        payload: Any,
        raw_text: str,
        request_params: Mapping[str, Any],
    ) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, Mapping):
            raise self._provider_error(
                f"Hero-SMS {action or 'request'} returned an unsupported response"
            )

        data = payload.get("data")
        if isinstance(data, Mapping):
            payload = data

        if action in {"getNumber", "getNumberV2"}:
            activation_id = _first_text(payload, "activationId", "activation_id", "id")
            phone = _phone_digits(
                payload.get("phoneNumber", payload.get("phone_number", payload.get("phone")))
            )
            if activation_id and phone:
                return f"ACCESS_NUMBER:{activation_id}:{phone}"

        if action in {"getStatus", "getStatusV2"}:
            code = _first_text(payload, "code", "smsCode", "sms_code")
            sms = payload.get("sms")
            if isinstance(sms, Mapping):
                code = code or _first_text(sms, "code", "smsCode", "sms_code")
            if isinstance(sms, list):
                for item in reversed(sms):
                    if isinstance(item, Mapping):
                        code = code or _first_text(item, "code", "smsCode", "sms_code")
                        if code:
                            break
            if code:
                return f"STATUS_OK:{code}"
            status = _first_text(payload, "status", "result", "response")
            if status:
                return status
            return "STATUS_WAIT_CODE"

        if action == "setStatus":
            result = _first_text(payload, "status", "result", "response", "message")
            if result:
                return result
            if payload.get("success") is True or payload.get("ok") is True:
                return {
                    "3": "ACCESS_RETRY_GET",
                    "6": "ACCESS_ACTIVATION",
                    "8": "ACCESS_CANCEL",
                }.get(str(request_params.get("status") or ""), "ACCESS_ACTIVATION")

        result = _first_text(payload, "result", "response", "status", "message")
        if result:
            return result
        if raw_text:
            return raw_text
        raise self._provider_error(f"Hero-SMS {action or 'request'} returned an empty response")

    @staticmethod
    def _error_from(payload: Any, raw_text: str) -> tuple[str, str] | None:
        if isinstance(payload, Mapping):
            title = _first_text(payload, "title", "error", "errorCode", "error_code")
            details = _first_text(payload, "details", "message", "error_description")
            if title:
                return title.upper(), details

        text = str(payload if isinstance(payload, str) else raw_text or "").strip()
        upper = text.upper()
        known = (
            "BAD_KEY",
            "INVALID_KEY",
            "WRONG_KEY",
            "NO_BALANCE",
            "NOT_ENOUGH_BALANCE",
            "NO_NUMBERS",
            "SERVICE_UNAVAILABLE_REGION",
            "BAD_ACTION",
            "BAD_SERVICE",
            "BAD_STATUS",
            "NO_ACTIVATION",
            "WRONG_MAX_PRICE",
        )
        if upper in known:
            return upper, ""
        if upper.startswith("WRONG_MAX_PRICE:"):
            return "WRONG_MAX_PRICE", upper.split(":", 1)[1][:40]
        if upper.startswith("WRONG_MAX_PRICE"):
            return "WRONG_MAX_PRICE", ""
        if upper.startswith("THE SERVICE IS PROHIBITED"):
            return "SERVICE_PROHIBITED", text[:200]
        return None

    def _raise_error(self, title: str, details: str) -> None:
        safe_title = re.sub(r"[^A-Z0-9_-]", "_", title.upper())[:80] or "UNKNOWN_ERROR"
        safe_details = str(details or "").replace("\r", " ").replace("\n", " ")[:300]
        if self._api_key:
            safe_details = safe_details.replace(self._api_key, "[redacted]")
        safe_details = re.sub(r"(?i)api[_-]?key\s*[=:]\s*[^\s&]+", "api_key=[redacted]", safe_details)
        safe_details = re.sub(r"https?://\S+", "[redacted-url]", safe_details)
        message = f"Hero-SMS {safe_title}"
        if safe_details:
            message += f": {safe_details}"
        if safe_title in {"NO_BALANCE", "NOT_ENOUGH_BALANCE"}:
            raise self._no_balance_error(message)
        if safe_title == "NO_NUMBERS":
            raise self._no_numbers_error(message)
        raise self._provider_error(message)


def _env_price(name: str, legacy_name: str = "") -> Decimal | None:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw and legacy_name:
        raw = str(os.getenv(legacy_name, "") or "").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d{1,4})?", raw):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _price_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal("0.0001")).normalize(), "f")


def _env_boolean(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, "") or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class _ActivationRoute:
    status_action: str = "getStatus"
    country: str = ""
    acquired_at: float = field(default_factory=time.time)


class _RuntimeSmsCoordinator:
    """Temporary Hero-only lifecycle adapter over the upstream SMS module."""

    def __init__(
        self,
        module: ModuleType | Any,
        adapter: HeroSmsAdapter,
        original_functions: Mapping[str, Any],
    ) -> None:
        self.module = module
        self.adapter = adapter
        self.original_functions = dict(original_functions)
        self._lock = threading.RLock()
        self._routes: dict[str, _ActivationRoute] = {}
        self._country_cursor = 0

    def route_for(self, activation_id: Any) -> _ActivationRoute | None:
        key = str(activation_id or "").strip()
        with self._lock:
            return self._routes.get(key)

    def remember(self, activation_id: Any, route: _ActivationRoute) -> None:
        key = str(activation_id or "").strip()
        if key:
            with self._lock:
                self._routes[key] = route

    def forget(self, activation_id: Any) -> None:
        key = str(activation_id or "").strip()
        if key:
            with self._lock:
                self._routes.pop(key, None)

    def request_dispatch(self, http: Any, params: Mapping[str, Any]) -> str:
        request_params = dict(params)
        route = self.route_for(request_params.get("id"))
        if (
            request_params.get("action") == "getStatus"
            and route is not None
            and route.status_action == "getStatusV2"
        ):
            request_params["action"] = "getStatusV2"
        return self.adapter.request(http, request_params)

    def _hero_countries(self, requested_country: Any = None) -> list[str]:
        fallback = [requested_country] if str(requested_country or "").strip() else []
        configured = os.getenv("HERO_SMS_COUNTRIES", "")
        try:
            countries = normalize_hero_countries(configured, fallback=fallback)
        except ValueError:
            countries = []
        requested = str(requested_country or "").strip()
        if requested.isdigit():
            requested = str(int(requested))
            countries = [requested, *(item for item in countries if item != requested)]
        elif countries:
            with self._lock:
                cursor = self._country_cursor % len(countries)
            countries = [*countries[cursor:], *countries[:cursor]]
        if not countries:
            provider_error = getattr(self.module, "SmsProviderError", HeroSmsError)
            raise provider_error("Hero-SMS requires at least one configured country")
        return countries[:10]

    def _advance_country_cursor(self, acquired_country: str) -> None:
        try:
            configured = normalize_hero_countries(os.getenv("HERO_SMS_COUNTRIES", ""))
        except ValueError:
            configured = []
        if not configured:
            return
        try:
            next_index = (configured.index(str(acquired_country)) + 1) % len(configured)
        except ValueError:
            return
        with self._lock:
            self._country_cursor = next_index

    def _hero_price_candidates(self, http: Any, country: str) -> tuple[list[Decimal], bool]:
        payloads: list[Any] = []
        for action, extras in (
            ("getPricesExtended", {"freePrice": "true"}),
            ("getPrices", {}),
        ):
            try:
                payloads.append(
                    self.adapter.query(
                        http,
                        {
                            "action": action,
                            "service": HERO_SMS_SERVICE_CODE,
                            "country": country,
                            **extras,
                        },
                    )
                )
            except Exception:
                # The two catalog actions have uneven regional support. A
                # failed lookup may fall back to a hard-capped getNumber, but
                # never to an uncapped request when max_price is configured.
                continue

        if not payloads:
            return [], False
        tiers = merge_price_tiers(payloads)
        minimum = _env_price("HERO_SMS_MIN_PRICE")
        maximum = _env_price("HERO_SMS_MAX_PRICE", "SMS_MAX_PRICE")
        filtered = filter_price_tiers(
            tiers,
            min_price=_price_text(minimum),
            max_price=_price_text(maximum),
        )
        prices: list[Decimal] = []
        for row in filtered:
            if not row.get("available"):
                continue
            try:
                value = Decimal(str(row.get("price") or ""))
            except InvalidOperation:
                continue
            if value > 0 and value not in prices:
                prices.append(value)
        return sorted(prices), True

    @staticmethod
    def _preferred_first(prices: list[Decimal], preferred: Decimal | None) -> list[Decimal]:
        if preferred is None:
            return prices
        return [preferred, *(price for price in prices if price != preferred)]

    def acquire_hero(
        self,
        http: Any,
        *,
        service: str | None = None,
        country: str | None = None,
    ) -> tuple[str, str, _ActivationRoute]:
        countries = self._hero_countries(country)
        minimum = _env_price("HERO_SMS_MIN_PRICE")
        maximum = _env_price("HERO_SMS_MAX_PRICE", "SMS_MAX_PRICE")
        preferred = _env_price("HERO_SMS_PREFERRED_PRICE")
        if preferred is not None and (
            (minimum is not None and preferred < minimum)
            or (maximum is not None and preferred > maximum)
        ):
            preferred = None
        priority = str(os.getenv("HERO_SMS_ACQUIRE_PRIORITY", "country") or "country").strip().lower()
        if priority not in {"country", "price", "price_high"}:
            priority = "country"

        attempts: list[dict[str, Any]] = []
        for index, country_id in enumerate(countries):
            prices, catalog_known = self._hero_price_candidates(http, country_id)
            if priority == "price_high":
                prices = list(reversed(prices))
            prices = self._preferred_first(prices, preferred)
            rank = (
                (max(prices) if priority == "price_high" and prices else None)
                or (min(prices) if prices else None)
            )
            attempts.append(
                {
                    "index": index,
                    "country": country_id,
                    "prices": prices,
                    "catalog_known": catalog_known,
                    "rank": rank,
                }
            )

        if priority in {"price", "price_high"}:
            attempts.sort(
                key=lambda item: (
                    item["rank"] is None,
                    -item["rank"] if priority == "price_high" and item["rank"] is not None else item["rank"] or Decimal(0),
                    item["index"],
                )
            )

        last_error: Exception | None = None
        for attempt in attempts:
            prices: list[Decimal | None] = list(attempt["prices"])
            if not prices:
                # A minimum cannot be enforced by Hero's legacy getNumber API
                # without a known fixed tier, so skip rather than accidentally
                # buying below it. With only a maximum, a capped legacy probe
                # remains safe and preserves compatibility.
                if minimum is not None:
                    continue
                prices = [None]
            for price in prices:
                for action in ("getNumber", "getNumberV2"):
                    params: dict[str, Any] = {
                        "action": action,
                        "service": service or HERO_SMS_SERVICE_CODE,
                        "country": attempt["country"],
                    }
                    if price is not None:
                        params["maxPrice"] = _price_text(price)
                        params["fixedPrice"] = "true"
                    elif maximum is not None:
                        params["maxPrice"] = _price_text(maximum)
                    logger = getattr(self.module, "logger", None)
                    if logger is not None:
                        logger.info(
                            "[SMS:Hero] trying country=%s price=%s action=%s priority=%s",
                            attempt["country"],
                            _price_text(price) if price is not None else (
                                f"<= {_price_text(maximum)}" if maximum is not None else "auto"
                            ),
                            action,
                            priority,
                        )
                    try:
                        text = self.adapter.request(http, params)
                    except Exception as exc:
                        last_error = exc
                        if isinstance(exc, getattr(self.module, "SmsNoNumbersError", ())):
                            continue
                        # A price-specific rejection may still succeed on the
                        # next catalog tier/country. If every Hero attempt
                        # fails, the final Hero error is returned to the job.
                        continue
                    if not text.startswith("ACCESS_NUMBER:"):
                        last_error = getattr(self.module, "SmsProviderError", HeroSmsError)(
                            "Hero-SMS getNumber returned an unexpected response"
                        )
                        continue
                    parts = text.split(":", 2)
                    if len(parts) != 3 or not parts[1].strip() or not _phone_digits(parts[2]):
                        last_error = getattr(self.module, "SmsProviderError", HeroSmsError)(
                            "Hero-SMS getNumber returned an invalid activation"
                        )
                        continue
                    activation_id = parts[1].strip()
                    phone = _phone_digits(parts[2])
                    acquired = getattr(self.module, "_ACQUIRED_AT", None)
                    if isinstance(acquired, dict):
                        acquired[activation_id] = time.time()
                    self._advance_country_cursor(attempt["country"])
                    logger = getattr(self.module, "logger", None)
                    if logger is not None:
                        logger.info(
                            "[SMS:Hero] acquired country=%s price=%s action=%s activation_id=%s",
                            attempt["country"],
                            _price_text(price) if price is not None else "auto",
                            action,
                            activation_id,
                        )
                    return activation_id, phone, _ActivationRoute(
                        status_action="getStatusV2" if action == "getNumberV2" else "getStatus",
                        country=attempt["country"],
                    )
        if last_error is not None:
            raise last_error
        no_numbers = getattr(self.module, "SmsNoNumbersError", HeroSmsNoNumbersError)
        raise no_numbers("Hero-SMS no numbers within the configured country/price range")

    def acquire_number(self, http: Any = None, service: str | None = None, country: str | None = None):
        own_http = http is None
        http = http or self.module._http()
        try:
            activation_id, phone, route = self.acquire_hero(
                http,
                service=service,
                country=country,
            )
            self.remember(activation_id, route)
            return activation_id, phone
        finally:
            if own_http:
                try:
                    http.close()
                except Exception:
                    pass

    def _routed_call(self, name: str, activation_id: Any, *args: Any, **kwargs: Any):
        original = self.original_functions[name]
        return original(activation_id, *args, **kwargs)

    def wait_for_sms_code(self, activation_id: Any, *args: Any, **kwargs: Any):
        return self._routed_call("wait_for_sms_code", activation_id, *args, **kwargs)

    def set_status(self, activation_id: Any, status: int, *args: Any, **kwargs: Any):
        result = self._routed_call("set_status", activation_id, status, *args, **kwargs)
        if int(status) in {6, 8}:
            self.forget(activation_id)
        return result

    def complete(self, activation_id: Any, *args: Any, **kwargs: Any):
        try:
            return self._routed_call("complete", activation_id, *args, **kwargs)
        finally:
            self.forget(activation_id)

    def cancel(self, activation_id: Any, *args: Any, **kwargs: Any):
        route = self.route_for(activation_id)
        if route is None:
            acquired = getattr(self.module, "_ACQUIRED_AT", {})
            acquired_at = acquired.get(str(activation_id)) if isinstance(acquired, dict) else None
            route = _ActivationRoute(
                acquired_at=float(acquired_at) if acquired_at is not None else time.time()
            )
        supplied_http = kwargs.get("http") or (args[0] if args else None)
        background = args[1] if len(args) > 1 else kwargs.get("background", True)

        def cancel_hero() -> None:
            min_delay = max(0, int(getattr(self.module, "_MIN_CANCEL_DELAY", 125) or 125))
            remaining = min_delay - (time.time() - route.acquired_at)
            if remaining > 0:
                time.sleep(remaining)
            http = supplied_http if not background and supplied_http is not None else self.module._http()
            own_http = http is not supplied_http
            try:
                for attempt in range(2):
                    try:
                        self.adapter.request(
                            http,
                            {"action": "setStatus", "status": "8", "id": str(activation_id)},
                        )
                        self.forget(activation_id)
                        acquired = getattr(self.module, "_ACQUIRED_AT", None)
                        if isinstance(acquired, dict):
                            acquired.pop(str(activation_id), None)
                        return
                    except Exception as exc:
                        if attempt == 0:
                            time.sleep(5)
                        else:
                            logger = getattr(self.module, "logger", None)
                            if logger is not None:
                                logger.warning("[SMS:Hero] cancel failed (%s)", type(exc).__name__)
            finally:
                if own_http:
                    try:
                        http.close()
                    except Exception:
                        pass

        if background:
            threading.Thread(
                target=cancel_hero,
                name=f"sms-cancel-hero-{activation_id}",
                daemon=True,
            ).start()
            return None
        cancel_hero()
        return None


_PATCH_LOCK = threading.RLock()


@dataclass
class HeroSmsPatch:
    """Handle that restores every exact upstream function replaced at install."""

    module: ModuleType | Any
    original_request: Any
    patched_request: Any
    original_functions: dict[str, Any] = field(default_factory=dict)
    patched_functions: dict[str, Any] = field(default_factory=dict)
    coordinator: Any = field(default=None, repr=False)
    _restored: bool = field(default=False, init=False, repr=False)

    def restore(self) -> None:
        with _PATCH_LOCK:
            if self._restored:
                return
            for name, original in self.original_functions.items():
                if getattr(self.module, name, None) is self.patched_functions.get(name):
                    setattr(self.module, name, original)
            if getattr(self.module, "_request_grizzly", None) is self.patched_request:
                self.module._request_grizzly = self.original_request
            self._restored = True


def install_hero_sms_patch(sms_provider: ModuleType | Any) -> HeroSmsPatch:
    """Install the temporary Hero-only adapter over the upstream lifecycle."""

    provider = str(sms_provider._provider() or "").strip().lower()
    if provider != "hero":
        raise RuntimeError("Hero SMS is the only supported SMS provider")

    adapter = HeroSmsAdapter(
        os.getenv("HERO_SMS_API_KEY", ""),
        provider_error=sms_provider.SmsProviderError,
        no_numbers_error=sms_provider.SmsNoNumbersError,
        no_balance_error=sms_provider.SmsNoBalanceError,
    )
    logger = getattr(sms_provider, "logger", None)
    if logger is not None:
        logger.info(
            "[SMS:Hero] API transport=%s",
            "proxy" if adapter._proxy_url else "direct",
        )

    with _PATCH_LOCK:
        original = sms_provider._request_grizzly
        lifecycle_names = (
            "acquire_number",
            "wait_for_sms_code",
            "set_status",
            "complete",
            "cancel",
        )
        original_functions = {
            name: getattr(sms_provider, name)
            for name in lifecycle_names
            if callable(getattr(sms_provider, name, None))
        }
        coordinator = _RuntimeSmsCoordinator(
            sms_provider,
            adapter,
            original_functions,
        )

        def hero_request(http: Any, params: Mapping[str, Any]) -> str:
            return coordinator.request_dispatch(http, params)

        sms_provider._request_grizzly = hero_request
        patched_functions: dict[str, Any] = {}
        for name in lifecycle_names:
            if name not in original_functions:
                continue
            patched = getattr(coordinator, name)
            patched_functions[name] = patched
            setattr(sms_provider, name, patched)
        return HeroSmsPatch(
            module=sms_provider,
            original_request=original,
            patched_request=hero_request,
            original_functions=original_functions,
            patched_functions=patched_functions,
            coordinator=coordinator,
        )


__all__ = [
    "HERO_SMS_API_BASE",
    "HeroSmsAdapter",
    "HeroSmsError",
    "HeroSmsNoBalanceError",
    "HeroSmsNoNumbersError",
    "HeroSmsPatch",
    "install_hero_sms_patch",
]
