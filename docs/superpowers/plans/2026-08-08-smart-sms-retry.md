# Smart SMS Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在统一管理器入口中加入按失败原因决策的短信换号、风控熔断和非自动重试策略，同时保持两个上游项目文件可同步更新。

**Architecture:** 纯策略放在 `manager/sms_retry_policy.py`，上游手机号验证的临时运行时覆盖放在 `manager/sms_runtime_overlay.py`，进程安装和调度错误映射放在 `manager/sms_bootstrap.py`。`manager_app.py` 只在统一管理器主进程和 Windows `__mp_main__` worker 导入阶段安装覆盖；所有 receiver 上游清单文件和 vendored 文件保持原样。

**Tech Stack:** Python 3、pytest、Flask、Windows `multiprocessing` spawn、现有 HeroSMS 生命周期适配器、PowerShell 本地启停脚本。

## Global Constraints

- 第一次 `fraud_guard` 或手机号 `rate_limited` 必须停止当前账号任务，且不得由调度器自动重跑。
- `number_in_use`、短信收码超时、OTP 拒绝和未知发送错误最多换号一次。
- `NO_NUMBERS`、HeroSMS 余额不足和凭据错误必须立即停止。
- `SMS_MAX_RETRIES` 继续作为绝对总上限，任何策略分支不得超过它。
- 不实现代理轮换、指纹伪装或风控绕过功能。
- 不自动修改国家、最低价、最高价或指定价格档。
- 不修改 `src/`、`vendor/turb-gpt-free-register/` 或其他 receiver 上游清单文件。
- 运行日志不得新增完整手机号、短信验证码、API Key 或原始敏感响应。
- 直接运行 `python app.py` 保持上游原始策略；优化只承诺作用于 `启动.cmd` / `manager_app.py`。

---

### Task 1: 纯短信重试策略

**Files:**
- Create: `manager/sms_retry_policy.py`
- Create: `tests/test_sms_retry_policy.py`

**Interfaces:**
- Produces: `SmsFailure(str, Enum)`，包含 `FRAUD_GUARD`、`PHONE_RATE_LIMITED`、`NUMBER_IN_USE`、`NUMBER_REJECTED`、`SMS_TIMEOUT`、`OTP_REJECTED`、`NO_NUMBERS`、`NO_BALANCE`、`BAD_KEY`、`TRANSIENT_SERVER`、`UNKNOWN_SEND`。
- Produces: `RetryAction(str, Enum)`，取值为 `STOP` 和 `RETRY`。
- Produces: `RetryDecision(action: RetryAction, code: str, message: str)`。
- Produces: `classify_send_failure(text: str, status_code: int, upstream_reason: str = "") -> SmsFailure | None`。
- Produces: `decide_retry(failure: SmsFailure, *, occurrence: int, total_attempts: int, max_attempts: int) -> RetryDecision`。

- [ ] **Step 1: 写失败原因分类的失败测试**

```python
import pytest

from manager.sms_retry_policy import SmsFailure, classify_send_failure


@pytest.mark.parametrize(
    ("body", "status", "upstream", "expected"),
    [
        ("invalid_request_error fraud_guard suspicious behavior", 400, "send_rejected", SmsFailure.FRAUD_GUARD),
        ("invalid_request_error rate_limit_exceeded", 429, "send_limited", SmsFailure.PHONE_RATE_LIMITED),
        ("phone_number_in_use", 400, "phone_used_or_max", SmsFailure.NUMBER_IN_USE),
        ("", 503, "server_error", SmsFailure.TRANSIENT_SERVER),
        ("", 204, "", None),
    ],
)
def test_classify_send_failure(body, status, upstream, expected):
    assert classify_send_failure(body, status, upstream) is expected
```

- [ ] **Step 2: 运行分类测试并确认因模块尚不存在而失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sms_retry_policy.py::test_classify_send_failure -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.sms_retry_policy'`。

- [ ] **Step 3: 写决策上限的失败测试**

```python
from manager.sms_retry_policy import RetryAction, SmsFailure, decide_retry


@pytest.mark.parametrize("failure", [SmsFailure.FRAUD_GUARD, SmsFailure.PHONE_RATE_LIMITED, SmsFailure.NO_NUMBERS])
def test_terminal_failures_stop_on_first_attempt(failure):
    decision = decide_retry(failure, occurrence=1, total_attempts=1, max_attempts=10)
    assert decision.action is RetryAction.STOP


@pytest.mark.parametrize("failure", [SmsFailure.NUMBER_IN_USE, SmsFailure.SMS_TIMEOUT, SmsFailure.OTP_REJECTED, SmsFailure.UNKNOWN_SEND])
def test_retryable_failures_allow_only_one_replacement(failure):
    first = decide_retry(failure, occurrence=1, total_attempts=1, max_attempts=10)
    second = decide_retry(failure, occurrence=2, total_attempts=2, max_attempts=10)
    assert first.action is RetryAction.RETRY
    assert second.action is RetryAction.STOP


def test_global_retry_limit_wins_over_reason_limit():
    decision = decide_retry(SmsFailure.NUMBER_IN_USE, occurrence=1, total_attempts=1, max_attempts=1)
    assert decision.action is RetryAction.STOP
```

- [ ] **Step 4: 实现最小纯策略**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SmsFailure(str, Enum):
    FRAUD_GUARD = "fraud_guard"
    PHONE_RATE_LIMITED = "phone_rate_limited"
    NUMBER_IN_USE = "number_in_use"
    NUMBER_REJECTED = "number_rejected"
    SMS_TIMEOUT = "sms_timeout"
    OTP_REJECTED = "otp_rejected"
    NO_NUMBERS = "sms_no_numbers"
    NO_BALANCE = "sms_no_balance"
    BAD_KEY = "sms_bad_key"
    TRANSIENT_SERVER = "phone_transient_server"
    UNKNOWN_SEND = "phone_unknown_send"


class RetryAction(str, Enum):
    STOP = "stop"
    RETRY = "retry"


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    code: str
    message: str


_ATTEMPT_LIMITS = {
    SmsFailure.FRAUD_GUARD: 1,
    SmsFailure.PHONE_RATE_LIMITED: 1,
    SmsFailure.NO_NUMBERS: 1,
    SmsFailure.NO_BALANCE: 1,
    SmsFailure.BAD_KEY: 1,
    SmsFailure.NUMBER_IN_USE: 2,
    SmsFailure.NUMBER_REJECTED: 2,
    SmsFailure.SMS_TIMEOUT: 2,
    SmsFailure.OTP_REJECTED: 2,
    SmsFailure.TRANSIENT_SERVER: 2,
    SmsFailure.UNKNOWN_SEND: 2,
}


def classify_send_failure(text: str, status_code: int, upstream_reason: str = "") -> SmsFailure | None:
    body = f"{text} {upstream_reason}".casefold()
    if "fraud_guard" in body or "suspicious behavior" in body:
        return SmsFailure.FRAUD_GUARD
    if any(marker in body for marker in ("rate_limit", "rate limit", "too many", "send_limited")) or status_code == 429:
        return SmsFailure.PHONE_RATE_LIMITED
    if any(marker in body for marker in ("phone_number_in_use", "already in use", "already used", "phone_used_or_max")):
        return SmsFailure.NUMBER_IN_USE
    if upstream_reason in {"invalid_phone", "delivery_refused", "whatsapp_channel"}:
        return SmsFailure.NUMBER_REJECTED
    if status_code >= 500 or upstream_reason == "server_error":
        return SmsFailure.TRANSIENT_SERVER
    if status_code in {200, 204} and not upstream_reason:
        return None
    return SmsFailure.UNKNOWN_SEND


def decide_retry(failure: SmsFailure, *, occurrence: int, total_attempts: int, max_attempts: int) -> RetryDecision:
    limit = min(_ATTEMPT_LIMITS[failure], max(1, int(max_attempts)))
    retry = occurrence < limit and total_attempts < max(1, int(max_attempts))
    action = RetryAction.RETRY if retry else RetryAction.STOP
    suffix = "允许换号 1/1" if retry else "停止当前账号任务"
    return RetryDecision(action=action, code=failure.value, message=f"{failure.value}: {suffix}")
```

- [ ] **Step 5: 运行策略测试并补齐所有枚举分支**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sms_retry_policy.py -v`

Expected: PASS，且测试数量覆盖 11 个失败枚举的停止或有限重试行为。

- [ ] **Step 6: 提交纯策略**

```powershell
git add manager/sms_retry_policy.py tests/test_sms_retry_policy.py
git commit -m "feat: add smart SMS retry policy"
```

---

### Task 2: 上游手机号验证运行时覆盖

**Files:**
- Create: `manager/sms_runtime_overlay.py`
- Create: `tests/test_sms_runtime_overlay.py`

**Interfaces:**
- Consumes: Task 1 的 `SmsFailure`、`RetryAction`、`classify_send_failure()`、`decide_retry()`。
- Produces: `SmartSmsStop(RuntimeError)`，公开 `code: str`，字符串格式固定为 `[smart_sms:<code>] <message>`。
- Produces: `CodexSmsPatch.restore() -> None`。
- Produces: `install_codex_sms_overlay(codex_oauth: object) -> CodexSmsPatch`。
- Private helper: `_cancel_without_masking(provider: object, activation_id: str, http: object, logger: object) -> None`。
- Private helper: `_retry_or_stop(module: object, failure: SmsFailure, counters: Counter, total_attempts: int, max_attempts: int) -> None`。
- The patched verifier consumes the existing upstream module members `_do_phone_verification`, `_post_json`, `_response_text`, `_phone_failure_reason`, `_sleep_before_phone_retry`, `_sms_provider_name`, `_cfg`, `sms_provider`, and `logger`.

- [ ] **Step 1: 写风控、限流和无号码立即停止的失败测试**

```python
from collections import deque
from dataclasses import dataclass, field
import logging
from types import SimpleNamespace

import pytest

from manager.sms_runtime_overlay import SmartSmsStop, install_codex_sms_overlay


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


@dataclass
class Lifecycle:
    acquired: int = 0
    cancelled: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    statuses: list[tuple[str, int]] = field(default_factory=list)

    @property
    def acquire_calls(self) -> int:
        return self.acquired


@pytest.fixture
def fake_codex_module():
    def build(*, send=None, validate=None, sms_codes=None, wait_errors=None, acquire_error="", cancel_error=False, phone="15551234567"):
        lifecycle = Lifecycle()
        send_queue = deque(send or [(204, "")])
        validate_queue = deque(validate or [(200, "")])
        code_queue = deque(sms_codes or ["123456"])
        wait_queue = deque(wait_errors or [])

        class SmsProviderError(RuntimeError):
            pass

        class SmsNoNumbersError(SmsProviderError):
            pass

        class SmsNoBalanceError(SmsProviderError):
            pass

        class SmsCodeTimeout(SmsProviderError):
            pass

        class FakeHttp:
            def close(self):
                return None

        class FakeProvider:
            SmsProviderError = SmsProviderError
            SmsNoNumbersError = SmsNoNumbersError
            SmsNoBalanceError = SmsNoBalanceError
            SmsCodeTimeout = SmsCodeTimeout

            @staticmethod
            def _http():
                return FakeHttp()

            @staticmethod
            def acquire_number(http):
                lifecycle.acquired += 1
                if acquire_error == "NO_NUMBERS":
                    raise SmsNoNumbersError("NO_NUMBERS")
                if acquire_error == "NO_BALANCE":
                    raise SmsNoBalanceError("NO_BALANCE")
                return f"activation-{lifecycle.acquired}", phone

            @staticmethod
            def cancel(activation_id, http):
                if cancel_error:
                    raise RuntimeError("cancel failed")
                lifecycle.cancelled.append(activation_id)

            @staticmethod
            def set_status(activation_id, status, *, http):
                lifecycle.statuses.append((activation_id, status))

            @staticmethod
            def wait_for_sms_code(activation_id, http):
                if wait_queue and wait_queue.popleft() == "timeout":
                    raise SmsCodeTimeout("timeout")
                return code_queue.popleft()

            @staticmethod
            def complete(activation_id, http):
                lifecycle.completed.append(activation_id)

        def post_json(session, url, payload, *, referer):
            status, text = (validate_queue if "phone-otp/validate" in url else send_queue).popleft()
            return FakeResponse(status, text)

        def phone_failure_reason(text, status_code):
            body = text.casefold()
            if "rate_limit" in body:
                return "send_limited"
            if "phone_number_in_use" in body:
                return "phone_used_or_max"
            if status_code >= 500:
                return "server_error"
            if status_code >= 400:
                return "send_rejected"
            return ""

        module = SimpleNamespace(
            _cfg=SimpleNamespace(SMS_MAX_RETRIES=10),
            _do_phone_verification=lambda session: None,
            _post_json=post_json,
            _response_text=lambda response: response.text,
            _phone_failure_reason=phone_failure_reason,
            _sleep_before_phone_retry=lambda attempt, maximum: None,
            _sms_provider_name=lambda: "hero",
            sms_provider=FakeProvider,
            logger=logging.getLogger("test-managed-sms"),
        )
        return module, lifecycle

    return build


@pytest.mark.parametrize(
    ("response_text", "status", "expected_code"),
    [
        ("invalid_request_error fraud_guard suspicious behavior", 400, "fraud_guard"),
        ("invalid_request_error rate_limit_exceeded", 429, "phone_rate_limited"),
    ],
)
def test_send_terminal_failure_cancels_once_and_stops(fake_codex_module, response_text, status, expected_code):
    module, lifecycle = fake_codex_module(send=[(status, response_text)])
    patch = install_codex_sms_overlay(module)
    try:
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    finally:
        patch.restore()
    assert caught.value.code == expected_code
    assert lifecycle.acquired == 1
    assert lifecycle.cancelled == ["activation-1"]
    assert lifecycle.completed == []


def test_no_numbers_stops_after_one_provider_scan(fake_codex_module):
    module, lifecycle = fake_codex_module(acquire_error="NO_NUMBERS")
    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    assert caught.value.code == "sms_no_numbers"
    assert lifecycle.acquire_calls == 1
```

- [ ] **Step 2: 运行终止测试并确认缺少运行时模块**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sms_runtime_overlay.py -k "terminal or no_numbers" -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.sms_runtime_overlay'`。

- [ ] **Step 3: 写有限换号、成功路径和取消失败测试**

```python
def test_number_in_use_retries_once_then_stops(fake_codex_module):
    module, lifecycle = fake_codex_module(send=[(400, "phone_number_in_use"), (400, "phone_number_in_use")])
    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    assert caught.value.code == "number_in_use"
    assert lifecycle.acquired == 2
    assert lifecycle.cancelled == ["activation-1", "activation-2"]


def test_sms_timeout_retries_once_then_stops(fake_codex_module):
    module, lifecycle = fake_codex_module(send=[(204, ""), (204, "")], wait_errors=["timeout", "timeout"])
    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    assert caught.value.code == "sms_timeout"
    assert lifecycle.acquired == 2


def test_success_completes_without_cancel(fake_codex_module):
    module, lifecycle = fake_codex_module(send=[(204, "")], sms_codes=["123456"], validate=[(200, "")])
    with install_codex_sms_overlay(module):
        module._do_phone_verification(object())
    assert lifecycle.statuses == [("activation-1", 1)]
    assert lifecycle.completed == ["activation-1"]
    assert lifecycle.cancelled == []


def test_cancel_failure_keeps_original_stop_code(fake_codex_module):
    module, lifecycle = fake_codex_module(send=[(400, "fraud_guard")], cancel_error=True)
    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    assert caught.value.code == "fraud_guard"
```

- [ ] **Step 4: 实现补丁句柄、兼容检查和稳定异常**

```python
class SmartSmsStop(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[smart_sms:{code}] {message}")


@dataclass
class CodexSmsPatch:
    module: object
    original: object
    patched: object
    restored: bool = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.restore()

    def restore(self) -> None:
        if not self.restored and getattr(self.module, "_do_phone_verification", None) is self.patched:
            setattr(self.module, "_do_phone_verification", self.original)
        if getattr(self.module, "_manager_smart_sms_patch", None) is self:
            delattr(self.module, "_manager_smart_sms_patch")
        self.restored = True


def install_codex_sms_overlay(codex_oauth: object) -> CodexSmsPatch:
    existing = getattr(codex_oauth, "_manager_smart_sms_patch", None)
    if isinstance(existing, CodexSmsPatch) and not existing.restored:
        return existing
    required = (
        "_do_phone_verification", "_post_json", "_response_text", "_phone_failure_reason",
        "_sleep_before_phone_retry", "_sms_provider_name", "_cfg", "sms_provider", "logger",
    )
    missing = [name for name in required if not hasattr(codex_oauth, name)]
    if missing:
        raise RuntimeError("智能接码覆盖不兼容，缺少上游接口: " + ", ".join(missing))
    original = codex_oauth._do_phone_verification
    patched = lambda session: _managed_phone_verification(codex_oauth, session)
    codex_oauth._do_phone_verification = patched
    patch = CodexSmsPatch(codex_oauth, original, patched)
    codex_oauth._manager_smart_sms_patch = patch
    return patch
```

- [ ] **Step 5: 实现受策略控制的验证循环**

实现 `_managed_phone_verification(module, session)`，必须按下面顺序调用现有上游和 HeroSMS 接口：

```python
http = provider._http()
try:
    for attempt in range(1, max_attempts + 1):
        activation_id, phone = provider.acquire_number(http)
        send_response = module._post_json(
            session,
            "https://auth.openai.com/api/accounts/add-phone/send",
            {"phone_number": f"+{phone}", "channel": "sms"},
            referer="https://auth.openai.com/add-phone",
        )
        send_text = module._response_text(send_response)
        upstream_reason = module._phone_failure_reason(send_text, send_response.status_code)
        failure = classify_send_failure(send_text, send_response.status_code, upstream_reason)
        if failure is not None:
            _cancel_without_masking(provider, activation_id, http, module.logger)
            _retry_or_stop(module, failure, counters, attempt, max_attempts)
            continue
        provider.set_status(activation_id, 1, http=http)
        sms_code = provider.wait_for_sms_code(activation_id, http)
        validate_response = module._post_json(
            session,
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            {"code": sms_code},
            referer="https://auth.openai.com/phone-verification",
        )
        if validate_response.status_code != 200:
            _cancel_without_masking(provider, activation_id, http, module.logger)
            _retry_or_stop(module, SmsFailure.OTP_REJECTED, counters, attempt, max_attempts)
            continue
        provider.complete(activation_id, http)
        module.logger.info("[Codex] 手机号验证通过")
        return
finally:
    http.close()
```

在上述主循环外补齐这些明确分支：

- `SmsNoNumbersError` -> `SmartSmsStop("sms_no_numbers", "HeroSMS 无可用号码：已扫描配置队列，停止任务")`，不再次调用取号。
- `SmsNoBalanceError` -> `SmartSmsStop("sms_no_balance", "HeroSMS 余额不足，停止当前账号任务")`。
- 包含 `BAD_KEY` 的 `SmsProviderError` -> `SmartSmsStop("sms_bad_key", "HeroSMS API Key 无效，停止当前账号任务")`。
- `SmsCodeTimeout` -> 取消并调用 `decide_retry(SmsFailure.SMS_TIMEOUT, occurrence=counters[SmsFailure.SMS_TIMEOUT], total_attempts=attempt, max_attempts=max_attempts)`。
- 其他 `SmsProviderError` -> `UNKNOWN_SEND`，最多重试一次。
- 所有重试日志只写失败代码、HTTP 状态和 `允许换号 1/1`，不写号码、验证码或原始响应。

两个私有 helper 使用以下实现形状：

```python
def _cancel_without_masking(provider, activation_id, http, logger) -> None:
    if not activation_id:
        return
    try:
        provider.cancel(activation_id, http)
    except Exception as exc:
        logger.warning("[Codex] smart_sms cancel failed (%s)", type(exc).__name__)


def _retry_or_stop(module, failure, counters, total_attempts, max_attempts) -> None:
    counters[failure] += 1
    decision = decide_retry(
        failure,
        occurrence=counters[failure],
        total_attempts=total_attempts,
        max_attempts=max_attempts,
    )
    module.logger.warning(
        "[Codex] smart_sms reason=%s action=%s message=%s",
        failure.value,
        decision.action.value,
        decision.message,
    )
    if decision.action is RetryAction.STOP:
        raise SmartSmsStop(decision.code, decision.message)
    module._sleep_before_phone_retry(total_attempts, max_attempts)
```

- [ ] **Step 6: 增加幂等、兼容性和脱敏日志测试**

```python
def test_install_is_idempotent_and_restore_is_safe(fake_codex_module):
    module, _ = fake_codex_module(send=[(204, "")], sms_codes=["123456"], validate=[(200, "")])
    first = install_codex_sms_overlay(module)
    second = install_codex_sms_overlay(module)
    assert second.patched is first.patched
    second.restore()
    first.restore()
    assert module._do_phone_verification is first.original


def test_missing_upstream_interface_fails_closed():
    with pytest.raises(RuntimeError, match="缺少上游接口"):
        install_codex_sms_overlay(object())


def test_logs_do_not_include_phone_code_or_response_secret(fake_codex_module, caplog):
    module, _ = fake_codex_module(send=[(400, "fraud_guard secret-response")], phone="15551234567")
    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop):
            module._do_phone_verification(object())
    text = caplog.text
    assert "15551234567" not in text
    assert "secret-response" not in text
```

- [ ] **Step 7: 让安装和恢复通过幂等测试**

确认 Step 4 的 `_manager_smart_sms_patch` 标记逻辑满足：第二次安装返回同一个 `CodexSmsPatch`；任一引用调用 `restore()` 后恢复原函数并删除标记；再次 `restore()` 不改变状态。

- [ ] **Step 8: 运行运行时覆盖测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sms_runtime_overlay.py -v`

Expected: PASS；风控和限流各只 acquire/cancel 一次，允许换号分支最多 acquire 两次，成功分支 complete 一次且 cancel 零次。

- [ ] **Step 9: 提交运行时覆盖**

```powershell
git add manager/sms_runtime_overlay.py tests/test_sms_runtime_overlay.py
git commit -m "feat: stop unsafe SMS retry loops"
```

---

### Task 3: 统一管理器安装与调度结果映射

**Files:**
- Create: `manager/sms_bootstrap.py`
- Modify: `manager_app.py:1-30`
- Create: `tests/test_managed_sms_bootstrap.py`

**Interfaces:**
- Consumes: Task 2 的 `install_codex_sms_overlay()` 和 `CodexSmsPatch.restore()`。
- Produces: `ManagedSmsInstallation.restore() -> None`。
- Produces: `install_managed_sms_runtime() -> ManagedSmsInstallation`。
- Patches only `src.upstream_bridge.run_codex_only` and `src.codex_service.CodexJobManager._failure_info` in memory.

- [ ] **Step 1: 写 bridge 包装和恢复的失败测试**

```python
from types import SimpleNamespace

from manager.sms_bootstrap import install_managed_sms_runtime
from src import upstream_bridge


def test_managed_runtime_installs_overlay_for_phone_jobs(monkeypatch):
    events = []
    codex = SimpleNamespace()
    fake_original_run = lambda settings, mailbox, reauth=False: events.append("run") or {"ok": True}
    monkeypatch.setattr(upstream_bridge, "_ensure_upstream_imports", lambda settings: codex)
    monkeypatch.setattr("manager.sms_bootstrap.install_codex_sms_overlay", lambda module: SimpleNamespace(restore=lambda: events.append("restore")))
    monkeypatch.setattr(upstream_bridge, "run_codex_only", fake_original_run)

    installation = install_managed_sms_runtime()
    try:
        assert upstream_bridge.run_codex_only is not fake_original_run
        result = upstream_bridge.run_codex_only(object(), {"email": "owner@example.com"})
    finally:
        installation.restore()

    assert result == {"ok": True}
    assert events == ["run", "restore"]
    assert upstream_bridge.run_codex_only is fake_original_run
```

- [ ] **Step 2: 写手机号错误不可自动重试的失败测试**

```python
from src.codex_service import CodexJobManager


def test_phone_terminal_codes_are_not_scheduler_retryable():
    installation = install_managed_sms_runtime()
    try:
        assert CodexJobManager._failure_info("[smart_sms:fraud_guard] stopped") == ("fraud_guard", False, 0)
        assert CodexJobManager._failure_info("[smart_sms:phone_rate_limited] stopped") == ("phone_rate_limited", False, 0)
        assert CodexJobManager._failure_info("[smart_sms:sms_no_numbers] stopped") == ("sms_no_numbers", False, 0)
    finally:
        installation.restore()
```

- [ ] **Step 3: 运行 bootstrap 测试并确认模块缺失**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_managed_sms_bootstrap.py -k "managed_runtime or terminal_codes" -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.sms_bootstrap'`。

- [ ] **Step 4: 实现进程内 bridge 和调度器补丁**

```python
@dataclass
class ManagedSmsInstallation:
    bridge: object
    original_run: object
    patched_run: object
    job_manager_class: type
    original_failure_info: object
    patched_failure_info: object
    worker_module: object | None
    original_worker_run: object | None
    restored: bool = False

    def restore(self) -> None:
        global _INSTALLATION
        with _INSTALL_LOCK:
            if self.restored:
                return
            if self.bridge.run_codex_only is self.patched_run:
                self.bridge.run_codex_only = self.original_run
            if self.worker_module is not None and self.worker_module.run_codex_only is self.patched_run:
                self.worker_module.run_codex_only = self.original_worker_run
            if self.job_manager_class._failure_info is self.patched_failure_info:
                self.job_manager_class._failure_info = staticmethod(self.original_failure_info)
            self.restored = True
            if _INSTALLATION is self:
                _INSTALLATION = None


_INSTALL_LOCK = threading.RLock()
_INSTALLATION: ManagedSmsInstallation | None = None


def install_managed_sms_runtime() -> ManagedSmsInstallation:
    global _INSTALLATION
    from src import upstream_bridge
    from src.codex_service import CodexJobManager

    with _INSTALL_LOCK:
        if _INSTALLATION is not None and not _INSTALLATION.restored:
            return _INSTALLATION
        original_run = upstream_bridge.run_codex_only
        original_failure_info = CodexJobManager._failure_info

        def managed_run(settings, mailbox, *, reauth=False):
            patch = None
            if not reauth:
                codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
                patch = install_codex_sms_overlay(codex_oauth)
            try:
                return original_run(settings, mailbox, reauth=reauth)
            finally:
                if patch is not None:
                    patch.restore()

        def managed_failure_info(message, status="", http_status=None):
            text = f"{status} {message}".casefold()
            mappings = {
                "[smart_sms:fraud_guard]": ("fraud_guard", False, 0),
                "[smart_sms:phone_rate_limited]": ("phone_rate_limited", False, 0),
                "[smart_sms:sms_no_numbers]": ("sms_no_numbers", False, 0),
                "[smart_sms:sms_no_balance]": ("sms_no_balance", False, 0),
                "[smart_sms:sms_bad_key]": ("sms_bad_key", False, 0),
            }
            for marker, result in mappings.items():
                if marker in text:
                    return result
            return original_failure_info(message, status, http_status)

        upstream_bridge.run_codex_only = managed_run
        CodexJobManager._failure_info = staticmethod(managed_failure_info)
        worker_module = sys.modules.get("src.codex_worker")
        original_worker_run = getattr(worker_module, "run_codex_only", None) if worker_module else None
        if worker_module is not None:
            worker_module.run_codex_only = managed_run
        _INSTALLATION = ManagedSmsInstallation(
            bridge=upstream_bridge,
            original_run=original_run,
            patched_run=managed_run,
            job_manager_class=CodexJobManager,
            original_failure_info=original_failure_info,
            patched_failure_info=managed_failure_info,
            worker_module=worker_module,
            original_worker_run=original_worker_run,
        )
        return _INSTALLATION
```

`ManagedSmsInstallation.restore()` 必须仅在目标仍是本安装实例时恢复 bridge、已导入 worker 的函数引用和 `CodexJobManager._failure_info`，并允许重复调用。实现中用模块级 `threading.RLock` 和安装标记保证多次安装返回同一有效句柄，不重复包装。

- [ ] **Step 5: 修改统一管理器入口支持主进程和 Windows spawn**

在 `manager_app.py` 中导入 `install_managed_sms_runtime`，并按以下规则调用：

```python
def main() -> None:
    install_managed_sms_runtime()
    upstream_entry.create_app = create_managed_app
    upstream_entry.main()


if __name__ == "__mp_main__":
    install_managed_sms_runtime()

if __name__ == "__main__":
    main()
```

不要在普通 `import manager_app` 时安装，以免测试、脚本和 WSGI 导入产生全局副作用。

- [ ] **Step 6: 写 `__mp_main__` 导入和幂等测试**

```python
import runpy
from pathlib import Path


def test_manager_entry_installs_runtime_when_spawn_imports(monkeypatch):
    calls = []
    monkeypatch.setattr("manager.sms_bootstrap.install_managed_sms_runtime", lambda: calls.append("installed"))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "manager_app.py"), run_name="__mp_main__")
    assert calls == ["installed"]


def test_installation_is_idempotent():
    first = install_managed_sms_runtime()
    second = install_managed_sms_runtime()
    try:
        assert second is first
    finally:
        first.restore()
```

- [ ] **Step 7: 运行 bootstrap 与现有流水线测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_managed_sms_bootstrap.py tests/test_codex_pipeline.py -v`

Expected: PASS；现有邮箱超时、网络错误和普通 HTTP 429 的调度策略保持原样，只有 `[smart_sms:phone_rate_limited]` 被改为不可自动重试。

- [ ] **Step 8: 提交统一管理器安装**

```powershell
git add manager/sms_bootstrap.py manager_app.py tests/test_managed_sms_bootstrap.py
git commit -m "feat: install managed SMS retry controls"
```

---

### Task 4: 上游保护、文档、完整验证和本地重启

**Files:**
- Modify: `manager/upstreams.lock.json: protected_prefixes`
- Modify: `LOCAL-DEPLOYMENT.md: startup and update verification sections`
- Test: `tests/test_sms_retry_policy.py`
- Test: `tests/test_sms_runtime_overlay.py`
- Test: `tests/test_managed_sms_bootstrap.py`
- Test: existing complete Python and Node suites

**Interfaces:**
- Consumes: Tasks 1-3 的三个新测试文件和统一管理器入口。
- Produces: 上游更新保护清单、运维说明、已重启且健康的本地服务。

- [ ] **Step 1: 保护新增本地测试文件**

在 `manager/upstreams.lock.json` 的 `protected_prefixes` 增加：

```json
"tests/test_sms_retry_policy.py",
"tests/test_sms_runtime_overlay.py",
"tests/test_managed_sms_bootstrap.py"
```

`manager/` 已整体受保护，`manager_app.py` 和 `LOCAL-DEPLOYMENT.md` 已单独受保护，不新增重复条目。

- [ ] **Step 2: 更新本地运维文档**

在 `LOCAL-DEPLOYMENT.md` 明确记录：

```markdown
## 智能接码熔断

通过 `启动.cmd` 或 `ops/local/Start-Local.ps1` 启动时，统一管理器会启用本地智能接码覆盖层：风控和手机号限流立即停止当前账号；号码已使用和短信超时最多换号一次；无号码不重复扫描。直接运行 `python app.py` 不启用该覆盖层。

覆盖层只位于受保护的 `manager/` 集成层。上游更新后会运行 Python 测试验证 `core.codex_oauth` 兼容性；兼容测试失败时更新脚本会恢复旧文件、依赖和服务。
```

- [ ] **Step 3: 运行新增测试和敏感信息检查**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sms_retry_policy.py tests/test_sms_runtime_overlay.py tests/test_managed_sms_bootstrap.py -v`

Expected: PASS。

Run: `rg -n "15551234567|123456|secret-response" logs tests/test_sms_runtime_overlay.py`

Expected: 测试夹具可包含假数据，但新产生的日志文件中不存在这些值；不得输出真实 `.env` 内容。

- [ ] **Step 4: 运行完整 Python 测试**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS，0 failures。

- [ ] **Step 5: 运行转换器与管理器浏览器桥测试**

Run: `node vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js`

Expected: PASS。

Run: `node tests\manager-bridge.test.js`

Expected: PASS。

- [ ] **Step 6: 验证没有修改上游受同步文件**

Run: `git diff --name-only fa81cb1 --`

Expected paths are limited to:

```text
LOCAL-DEPLOYMENT.md
manager/sms_bootstrap.py
manager/sms_retry_policy.py
manager/sms_runtime_overlay.py
manager/upstreams.lock.json
manager_app.py
tests/test_managed_sms_bootstrap.py
tests/test_sms_retry_policy.py
tests/test_sms_runtime_overlay.py
docs/superpowers/plans/2026-08-08-smart-sms-retry.md
```

No path under `src/` or `vendor/` may appear.

- [ ] **Step 7: 用正式开关重启服务并做只读冒烟测试**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Stop-Local.ps1`

Expected: 现有项目进程安全停止，不删除 `data/`、`.env` 或日志。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Start-Local.ps1 -NoBrowser`

Expected: `[成功] 服务已启动：http://127.0.0.1:5015/manager`。

Run: `Invoke-RestMethod -Uri 'http://127.0.0.1:5015/health' -TimeoutSec 5 | ConvertTo-Json -Compress`

Expected: `{"ok":true,"service":"codex-auto-sms-receiver"}`。

不要发起真实接码任务；故障分支由假的 HTTP 和 HeroSMS 生命周期测试验证，避免额外购买号码。

- [ ] **Step 8: 检查日志与工作区并提交**

Run: `Get-Content -Raw .\logs\server.stderr.log`

Expected: empty。

Run: `git diff --check; git status --short`

Expected: no whitespace errors；仅显示本任务预期文件。

```powershell
git add LOCAL-DEPLOYMENT.md manager/upstreams.lock.json docs/superpowers/plans/2026-08-08-smart-sms-retry.md
git commit -m "docs: explain managed SMS retry safeguards"
```

---

## Final Verification Checklist

- [ ] 每个新增生产函数都由先失败后通过的测试覆盖。
- [ ] `fraud_guard`、手机号 `rate_limited` 和 `NO_NUMBERS` 的测试证明只发生一次 acquire 或一次完整 provider 扫描。
- [ ] 允许换号分支的测试证明最多两个号码。
- [ ] 成功路径证明 set status、wait、validate、complete 顺序不变。
- [ ] 调度器不自动重跑手机号风控和手机号限流任务。
- [ ] Windows `__mp_main__` 导入会安装覆盖层。
- [ ] 新日志不含完整手机号、验证码、API Key 或原始响应。
- [ ] `src/` 和 `vendor/` 没有任何修改。
- [ ] Python、Node 和本地健康检查全部通过。
- [ ] 本地服务通过统一管理器正式开关重新启动并保持健康。
