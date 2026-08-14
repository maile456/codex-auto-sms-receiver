from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import upstream_bridge
from src.hero_sms import (
    HERO_SMS_API_BASE,
    HeroSmsAdapter,
    HeroSmsError,
    HeroSmsNoBalanceError,
    HeroSmsNoNumbersError,
    install_hero_sms_patch,
)


class FakeResponse:
    def __init__(self, *, text="", status_code=200, payload=...):
        self.text = text
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if self.payload is ...:
            raise ValueError("not json")
        return self.payload


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        return self.response


def test_legacy_text_uses_official_endpoint_and_query_key():
    http = FakeHttp(FakeResponse(text="ACCESS_NUMBER:123:15551234567"))

    result = HeroSmsAdapter("hero-secret").request(
        http,
        {"action": "getNumber", "service": "openai", "country": "10"},
    )

    assert result == "ACCESS_NUMBER:123:15551234567"
    assert http.calls == [
        (
            HERO_SMS_API_BASE,
            {
                "action": "getNumber",
                "service": "dr",
                "country": "10",
                "api_key": "hero-secret",
            },
        )
    ]


def test_hero_normalizes_openai_service_alias_to_official_dr_code():
    http = FakeHttp(FakeResponse(text="ACCESS_NUMBER:123:15551234567"))
    HeroSmsAdapter("hero-secret").request(
        http, {"action": "getNumber", "service": "openai", "country": "187"}
    )
    assert http.calls[0][1]["service"] == "dr"


def test_legacy_status_one_is_local_compatibility_noop():
    http = FakeHttp(FakeResponse(text="should-not-be-used"))
    result = HeroSmsAdapter("hero-secret").request(
        http, {"action": "setStatus", "id": "1", "status": "1"}
    )
    assert result == "ACCESS_READY"
    assert http.calls == []


def test_configured_key_cannot_be_overridden_by_caller_params():
    http = FakeHttp(FakeResponse(text="STATUS_WAIT_CODE"))
    HeroSmsAdapter("configured-key").request(
        http,
        {"action": "getStatus", "id": "1", "api_key": "caller-key"},
    )
    assert http.calls[0][1]["api_key"] == "configured-key"


def test_network_error_does_not_expose_key():
    class FailingHttp:
        def get(self, url, params):
            raise RuntimeError(f"failed URL {url}?api_key={params['api_key']}")

    with pytest.raises(HeroSmsError) as raised:
        HeroSmsAdapter("must-not-leak").request(FailingHttp(), {"action": "getStatus"})
    assert "must-not-leak" not in str(raised.value)


@pytest.mark.parametrize(
    ("explicit_proxy", "proxy_pool", "expected"),
    [
        (
            "socks5h://127.0.0.1:7897",
            "http://fallback.example.test:8080",
            "socks5h://127.0.0.1:7897",
        ),
        (
            "",
            '["http://first.example.test:8080", "http://second.example.test:8080"]',
            "http://first.example.test:8080",
        ),
    ],
)
def test_hero_requests_use_explicit_proxy_or_proxy_pool_fallback(
    monkeypatch, explicit_proxy, proxy_pool, expected
):
    monkeypatch.setenv("HERO_SMS_PROXY", explicit_proxy)
    monkeypatch.setenv("PROXY_POOL", proxy_pool)

    class ProxyRequiredHttp(FakeHttp):
        def __init__(self):
            super().__init__(FakeResponse(text="ACCESS_BALANCE:1"))
            self.proxies = {}

        def get(self, url, params):
            if self.proxies.get("https") != expected:
                raise TimeoutError("direct TLS timeout")
            return super().get(url, params)

    http = ProxyRequiredHttp()
    result = HeroSmsAdapter("hero-secret").request(http, {"action": "getBalance"})

    assert result == "ACCESS_BALANCE:1"
    assert http.proxies == {"http": expected, "https": expected}


def test_json_number_is_normalized_to_legacy_contract():
    http = FakeHttp(
        FakeResponse(
            payload={
                "activationId": "635468024",
                "phoneNumber": "+1 (555) 123-4567",
                "activationCost": 0.25,
            }
        )
    )
    result = HeroSmsAdapter("secret").request(http, {"action": "getNumberV2"})
    assert result == "ACCESS_NUMBER:635468024:15551234567"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"sms": {"code": "123456"}}, "STATUS_OK:123456"),
        ({"status": "STATUS_WAIT_RESEND"}, "STATUS_WAIT_RESEND"),
        ({"sms": {"code": ""}}, "STATUS_WAIT_CODE"),
    ],
)
def test_json_status_is_normalized(payload, expected):
    http = FakeHttp(FakeResponse(payload=payload))
    result = HeroSmsAdapter("secret").request(http, {"action": "getStatus", "id": "1"})
    assert result == expected


def test_json_set_status_success_maps_requested_lifecycle_state():
    http = FakeHttp(FakeResponse(payload={"success": True}))
    result = HeroSmsAdapter("secret").request(
        http, {"action": "setStatus", "id": "1", "status": "8"}
    )
    assert result == "ACCESS_CANCEL"


@pytest.mark.parametrize(
    ("title", "error_type"),
    [
        ("NO_BALANCE", HeroSmsNoBalanceError),
        ("NO_NUMBERS", HeroSmsNoNumbersError),
        ("BAD_KEY", HeroSmsError),
    ],
)
def test_official_json_errors_map_to_provider_errors(title, error_type):
    http = FakeHttp(
        FakeResponse(
            status_code=402,
            payload={"title": title, "details": "official error details"},
        )
    )
    with pytest.raises(error_type, match=title):
        HeroSmsAdapter("secret").request(http, {"action": "getNumber"})


def test_patch_uses_isolated_key_upstream_exceptions_and_restores(monkeypatch):
    class UpstreamProviderError(RuntimeError):
        pass

    class UpstreamNoNumbers(UpstreamProviderError):
        pass

    class UpstreamNoBalance(UpstreamProviderError):
        pass

    def original_request(http, params):
        return "original"

    module = SimpleNamespace(
        _provider=lambda: "hero",
        _cfg=SimpleNamespace(SMS_API_KEY="unused-upstream-key"),
        _request_grizzly=original_request,
        SmsProviderError=UpstreamProviderError,
        SmsNoNumbersError=UpstreamNoNumbers,
        SmsNoBalanceError=UpstreamNoBalance,
    )
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-secret")

    patch = install_hero_sms_patch(module)
    assert patch is not None
    assert module._request_grizzly is not original_request
    http = FakeHttp(FakeResponse(text="NO_BALANCE"))
    with pytest.raises(UpstreamNoBalance):
        module._request_grizzly(http, {"action": "getNumber"})
    assert http.calls[0][1]["api_key"] == "hero-secret"

    patch.restore()
    patch.restore()
    assert module._request_grizzly is original_request


def test_non_hero_provider_fails_closed():
    module = SimpleNamespace(_provider=lambda: "unsupported")
    with pytest.raises(RuntimeError, match="only supported"):
        install_hero_sms_patch(module)


def test_upstream_bridge_restores_patch_when_codex_fails(monkeypatch, tmp_path):
    original_request = object()
    sms_provider = SimpleNamespace(
        _provider=lambda: "hero",
        _cfg=SimpleNamespace(SMS_API_KEY="secret"),
        _request_grizzly=original_request,
        SmsProviderError=HeroSmsError,
        SmsNoNumbersError=HeroSmsNoNumbersError,
        SmsNoBalanceError=HeroSmsNoBalanceError,
    )

    def fail_codex(*args, **kwargs):
        assert sms_provider._request_grizzly is not original_request
        raise RuntimeError("codex failed")

    codex_oauth = SimpleNamespace(
        sms_provider=sms_provider,
        run_codex_oauth=fail_codex,
    )
    cleaned = []
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-secret")
    monkeypatch.setattr(upstream_bridge, "_ensure_upstream_imports", lambda settings: codex_oauth)
    monkeypatch.setattr(
        upstream_bridge,
        "_generic_api_otp_provider",
        lambda mailbox: (lambda *args, **kwargs: "123456", lambda: cleaned.append(True)),
    )
    settings = SimpleNamespace(project_root=tmp_path, data_dir=tmp_path / "data")

    with pytest.raises(RuntimeError, match="codex failed"):
        upstream_bridge.run_codex_only(
            settings,
            {"email": "owner@example.com", "source": "generic_api", "code_url": "https://x"},
        )

    assert sms_provider._request_grizzly is original_request
    assert cleaned == [True]


class RoutingHttp:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params):
        query = dict(params)
        self.calls.append((url, query))
        return self.handler(query)

    def close(self):
        return None


def _coordinator_module(http, *, original_acquire=None):
    class UpstreamProviderError(RuntimeError):
        pass

    class UpstreamNoNumbers(UpstreamProviderError):
        pass

    class UpstreamNoBalance(UpstreamProviderError):
        pass

    module = SimpleNamespace()
    module._cfg = SimpleNamespace(SMS_PROVIDER="hero")
    module._provider = lambda: module._cfg.SMS_PROVIDER
    module._http = lambda: http
    module._ACQUIRED_AT = {}
    module._MIN_CANCEL_DELAY = 0
    module.SmsProviderError = UpstreamProviderError
    module.SmsNoNumbersError = UpstreamNoNumbers
    module.SmsNoBalanceError = UpstreamNoBalance
    module._request_grizzly = lambda _http, params: "ORIGINAL:" + str(params.get("action"))
    module.acquire_number = original_acquire or (lambda **kwargs: ("upstream-1", "15550001111"))

    def wait_for_sms_code(activation_id, http=None, **kwargs):
        return module._request_grizzly(
            http,
            {"action": "getStatus", "id": activation_id},
        )

    def set_status(activation_id, status, http=None, **kwargs):
        return module._request_grizzly(
            http,
            {"action": "setStatus", "id": activation_id, "status": str(status)},
        )

    module.wait_for_sms_code = wait_for_sms_code
    module.set_status = set_status
    module.complete = lambda activation_id, http=None: module.set_status(activation_id, 6, http=http)
    module.cancel = lambda activation_id, http=None, background=True: None
    return module


def test_runtime_strategy_sorts_countries_by_low_price_and_uses_v2_status(monkeypatch):
    def handler(params):
        action = params["action"]
        country = str(params.get("country") or "")
        if action in {"getPricesExtended", "getPrices"}:
            price = "0.09" if country == "33" else "0.07"
            return FakeResponse(payload={country: {"dr": {price: {"count": 2}}}})
        if action == "getNumber":
            return FakeResponse(text="NO_NUMBERS")
        if action == "getNumberV2":
            return FakeResponse(payload={"activationId": "v2-1", "phoneNumber": "+15551234567"})
        if action == "getStatusV2":
            return FakeResponse(payload={"sms": {"code": "246810"}})
        return FakeResponse(text="ACCESS_CANCEL")

    http = RoutingHttp(handler)
    module = _coordinator_module(http)
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "33,187")
    monkeypatch.setenv("HERO_SMS_MIN_PRICE", "0.05")
    monkeypatch.setenv("HERO_SMS_MAX_PRICE", "0.10")
    monkeypatch.setenv("HERO_SMS_ACQUIRE_PRIORITY", "price")

    patch = install_hero_sms_patch(module)
    activation_id, phone = module.acquire_number(http=http)

    assert (activation_id, phone) == ("v2-1", "15551234567")
    number_calls = [params for _, params in http.calls if params["action"] in {"getNumber", "getNumberV2"}]
    assert number_calls[0]["country"] == "187"
    assert number_calls[0]["maxPrice"] == "0.07"
    assert number_calls[0]["fixedPrice"] == "true"
    assert all(float(params["maxPrice"]) <= 0.10 for params in number_calls)
    assert module.wait_for_sms_code(activation_id, http=http) == "STATUS_OK:246810"
    assert any(params["action"] == "getStatusV2" for _, params in http.calls)
    patch.restore()


def test_runtime_strategy_falls_back_in_configured_country_order(monkeypatch):
    def handler(params):
        action = params["action"]
        country = str(params.get("country") or "")
        if action in {"getPricesExtended", "getPrices"}:
            return FakeResponse(payload={country: {"dr": {"0.08": {"count": 2}}}})
        if country == "33":
            return FakeResponse(text="NO_NUMBERS")
        if action == "getNumber":
            return FakeResponse(text="ACCESS_NUMBER:fallback-1:15551234567")
        return FakeResponse(text="STATUS_WAIT_CODE")

    http = RoutingHttp(handler)
    module = _coordinator_module(http)
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "33,187")
    monkeypatch.setenv("HERO_SMS_ACQUIRE_PRIORITY", "country")
    monkeypatch.delenv("HERO_SMS_MIN_PRICE", raising=False)
    monkeypatch.setenv("HERO_SMS_MAX_PRICE", "0.10")
    monkeypatch.delenv("HERO_SMS_PREFERRED_PRICE", raising=False)

    patch = install_hero_sms_patch(module)
    assert module.acquire_number(http=http) == ("fallback-1", "15551234567")

    number_countries = [
        str(params["country"])
        for _, params in http.calls
        if params["action"] in {"getNumber", "getNumberV2"}
    ]
    assert number_countries == ["33", "33", "187"]
    patch.restore()


def test_runtime_strategy_rotates_to_next_country_after_acquired_number(monkeypatch):
    activations = []

    def handler(params):
        action = params["action"]
        country = str(params.get("country") or "")
        if action in {"getPricesExtended", "getPrices"}:
            return FakeResponse(payload={country: {"dr": {"0.08": {"count": 2}}}})
        if action == "getNumber":
            activations.append(country)
            return FakeResponse(text=f"ACCESS_NUMBER:rotation-{len(activations)}:15551234567")
        return FakeResponse(text="STATUS_WAIT_CODE")

    http = RoutingHttp(handler)
    module = _coordinator_module(http)
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "33,187,52")
    monkeypatch.setenv("HERO_SMS_ACQUIRE_PRIORITY", "country")
    monkeypatch.delenv("HERO_SMS_MIN_PRICE", raising=False)
    monkeypatch.setenv("HERO_SMS_MAX_PRICE", "0.10")
    monkeypatch.delenv("HERO_SMS_PREFERRED_PRICE", raising=False)

    patch = install_hero_sms_patch(module)
    assert module.acquire_number(http=http)[0] == "rotation-1"
    assert module.acquire_number(http=http)[0] == "rotation-2"
    assert module.acquire_number(http=http)[0] == "rotation-3"
    assert activations == ["33", "187", "52"]
    patch.restore()


def test_runtime_strategy_keeps_hard_cap_when_price_lookup_fails(monkeypatch):
    def handler(params):
        if params["action"] in {"getPricesExtended", "getPrices"}:
            raise RuntimeError("catalog network failure")
        if params["action"] == "getNumber":
            return FakeResponse(text="ACCESS_NUMBER:hard-cap:15551234567")
        return FakeResponse(text="STATUS_WAIT_CODE")

    http = RoutingHttp(handler)
    module = _coordinator_module(http)
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "33")
    monkeypatch.delenv("HERO_SMS_MIN_PRICE", raising=False)
    monkeypatch.setenv("HERO_SMS_MAX_PRICE", "0.12")

    patch = install_hero_sms_patch(module)
    assert module.acquire_number(http=http) == ("hard-cap", "15551234567")
    request = next(params for _, params in http.calls if params["action"] == "getNumber")
    assert request["maxPrice"] == "0.12"
    assert "fixedPrice" not in request
    patch.restore()


def test_runtime_strategy_never_calls_upstream_provider_on_hero_failure(monkeypatch):
    upstream_calls = []

    def original_acquire(http=None, service=None, country=None):
        upstream_calls.append((service, country))
        return "upstream-1", "15550001111"

    def handler(params):
        if params["action"] in {"getPricesExtended", "getPrices"}:
            return FakeResponse(payload={"33": {"dr": {"0.08": {"count": 1}}}})
        return FakeResponse(text="NO_NUMBERS")

    http = RoutingHttp(handler)
    module = _coordinator_module(http, original_acquire=original_acquire)
    monkeypatch.setenv("HERO_SMS_API_KEY", "hero-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "33")
    monkeypatch.setenv("HERO_SMS_MAX_PRICE", "0.10")

    patch = install_hero_sms_patch(module)
    with pytest.raises(module.SmsNoNumbersError, match="NO_NUMBERS"):
        module.acquire_number(http=http)
    assert upstream_calls == []
    patch.restore()
