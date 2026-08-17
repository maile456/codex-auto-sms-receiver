import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = PROJECT_ROOT / "vendor" / "turb-gpt-free-register"
if str(UPSTREAM_ROOT) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_ROOT))

from core import generic_api_mail_client as mail_client  # noqa: E402


PAGE_URL = "https://mail.example.test/messages/demo-token/owner@example.test"
STRUCTURED_API_URL = (
    "https://icloud.example.test/api/v1/code"
    "?email=owner%40example.test&key=test-api-key"
)
DETAIL_SUFFIX = "/demo-token/owner@example.test"
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "generic-mail-test",
}


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        status_code: int = 200,
        text: str = "",
        json_data=None,
        content_type: str = "text/html; charset=utf-8",
    ):
        self.url = url
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self._json_data is None:
            raise ValueError("response does not contain JSON")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses[url]


def test_plain_html_page_extracts_contextual_six_digit_code():
    page_url = "https://mail.example.test/code/token"
    session = FakeSession(
        {
            page_url: FakeResponse(
                url=page_url,
                text=(
                    "<html><body><p>Order 123456</p>"
                    "<p>Your verification code is <b>654321</b></p></body></html>"
                ),
            ),
        }
    )

    code = mail_client._fetch_current_code(session, page_url, HEADERS)

    assert code == "654321"
    assert [url for url, _kwargs in session.calls] == [page_url]


def _latest_mail_html(code: str, received_at: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-CN">
      <body>
        <main class="wrap">
          <h1>最新邮件</h1>
          <section class="panel">
            <div class="meta">
              <div class="subject">ChatGPT temporary login code</div>
              <div class="row"><div class="label">时间</div><div class="value">{received_at}</div></div>
            </div>
            <div class="content">
              <style>body {{ color: #111827; }}</style>
              <p>Your verification code is <strong>{code}</strong></p>
            </div>
          </section>
        </main>
      </body>
    </html>
    """


def test_latest_mail_page_accepts_fresh_timestamped_otp():
    page_url = "https://mail.example.test/mailbox/demo-token"
    session = FakeSession(
        {
            page_url: FakeResponse(
                url=page_url,
                text=_latest_mail_html("654321", "2026-08-15 02:13:16"),
            ),
        }
    )
    after_ts = datetime(
        2026, 8, 15, 2, 13, 14, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        page_url,
        HEADERS,
        after_ts=after_ts,
    )

    assert code == "654321"


def test_latest_mail_page_rejects_otp_from_before_current_login():
    page_url = "https://mail.example.test/mailbox/demo-token"
    session = FakeSession(
        {
            page_url: FakeResponse(
                url=page_url,
                text=_latest_mail_html("123456", "2026-08-15 02:12:00"),
            ),
        }
    )
    after_ts = datetime(
        2026, 8, 15, 2, 13, 14, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        page_url,
        HEADERS,
        after_ts=after_ts,
    )

    assert code is None


def _inbox_html(items: list[tuple[str, str]], *, detail_base: str = "/message/") -> str:
    rows = "\n".join(
        (
            f'<a class="item" data-id="{message_id}">'
            f'<div class="subject">{subject}</div>'
            "</a>"
        )
        for message_id, subject in items
    )
    return f"""
    <!doctype html>
    <html>
      <body>
        <section id="messages">{rows}</section>
        <script>
          const detailBase = {detail_base!r};
          const detailSuffix = {DETAIL_SUFFIX!r};
        </script>
      </body>
    </html>
    """


def _detail_url(message_id: str) -> str:
    return (
        "https://mail.example.test/message/"
        f"{quote(message_id, safe='')}{DETAIL_SUFFIX}"
    )


def _detail_response(
    url: str,
    body: str,
    *,
    received_at: str | int | None = None,
) -> FakeResponse:
    payload = {
        "body": body,
        "fromAddress": "noreply@openai.example.test",
        "html": True,
        "subject": "Your ChatGPT verification code",
    }
    if received_at is not None:
        payload["receivedAt"] = received_at
    return FakeResponse(
        url=url,
        text="",
        json_data=payload,
        content_type="application/json",
    )


def _structured_api_response(payload: dict) -> FakeResponse:
    return FakeResponse(
        url=STRUCTURED_API_URL,
        text=json.dumps(payload),
        json_data=payload,
        content_type="application/json",
    )


def test_html_inbox_does_not_treat_six_digit_message_ids_as_otp():
    page = _inbox_html(
        [
            ("481902", "Account notice"),
            ("390174", "Your subscription receipt"),
        ]
    )
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
        }
    )

    code = mail_client._fetch_current_code(session, PAGE_URL, HEADERS)

    assert code is None
    assert [url for url, _kwargs in session.calls] == [PAGE_URL]


def test_html_inbox_selects_newest_otp_message_and_decodes_base64_data_uri():
    selected_id = "390174"
    older_id = "208563"
    page = _inbox_html(
        [
            ("481902", "Account notice"),
            (selected_id, "Your ChatGPT verification code"),
            (older_id, "Older verification code"),
        ]
    )
    selected_url = _detail_url(selected_id)
    older_url = _detail_url(older_id)
    encoded_body = base64.b64encode(
        b"<html><body>Your verification code is <strong>654321</strong></body></html>"
    ).decode("ascii")
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
            selected_url: _detail_response(
                selected_url,
                f"data:text/html;charset=utf-8;base64,{encoded_body}",
            ),
            older_url: _detail_response(
                older_url,
                "data:text/plain,Your%20verification%20code%20is%20123456",
            ),
        }
    )

    code = mail_client._fetch_current_code(session, PAGE_URL, HEADERS)

    assert code == "654321"
    assert [url for url, _kwargs in session.calls] == [PAGE_URL, selected_url]


def test_html_inbox_decodes_percent_encoded_data_uri():
    message_id = "481902"
    detail_url = _detail_url(message_id)
    page = _inbox_html([(message_id, "OpenAI verification code")])
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
            detail_url: _detail_response(
                detail_url,
                "data:text/plain;charset=utf-8,"
                "Your%20verification%20code%20is%20654321",
            ),
        }
    )

    code = mail_client._fetch_current_code(session, PAGE_URL, HEADERS)

    assert code == "654321"
    assert [url for url, _kwargs in session.calls] == [PAGE_URL, detail_url]


def test_html_inbox_rejects_cross_origin_detail_endpoint_without_requesting_it():
    foreign_base = "https://foreign.example.test/message/"
    page = _inbox_html(
        [("481902", "OpenAI verification code")],
        detail_base=foreign_base,
    )
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
        }
    )

    with pytest.raises(mail_client.GenericApiMailError, match="同源|跨域|origin"):
        mail_client._fetch_current_code(session, PAGE_URL, HEADERS)

    assert [url for url, _kwargs in session.calls] == [PAGE_URL]


def test_html_inbox_rejects_otp_received_before_current_login():
    message_id = "481902"
    detail_url = _detail_url(message_id)
    page = _inbox_html([(message_id, "OpenAI verification code")])
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
            detail_url: _detail_response(
                detail_url,
                "data:text/plain,Your%20verification%20code%20is%20123456",
                received_at="2026-07-31T00:00:00+00:00",
            ),
        }
    )
    after_ts = datetime(2026, 7, 31, 0, 1, tzinfo=timezone.utc).timestamp()

    code = mail_client._fetch_current_code(
        session,
        PAGE_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code is None


def test_html_inbox_uses_newest_fresh_otp_even_if_html_order_is_wrong():
    older_id = "481902"
    newest_id = "390174"
    older_url = _detail_url(older_id)
    newest_url = _detail_url(newest_id)
    page = _inbox_html(
        [
            (older_id, "OpenAI verification code"),
            (newest_id, "OpenAI verification code"),
        ]
    )
    session = FakeSession(
        {
            PAGE_URL: FakeResponse(url=PAGE_URL, text=page),
            older_url: _detail_response(
                older_url,
                "data:text/plain,Your%20verification%20code%20is%20123456",
                received_at="2026-07-31T00:01:02Z",
            ),
            newest_url: _detail_response(
                newest_url,
                "data:text/plain,Your%20verification%20code%20is%20654321",
                received_at="2026-07-31T00:01:08+00:00",
            ),
        }
    )
    after_ts = datetime(2026, 7, 31, 0, 1, tzinfo=timezone.utc).timestamp()

    code = mail_client._fetch_current_code(
        session,
        PAGE_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code == "654321"
    assert [url for url, _kwargs in session.calls] == [
        PAGE_URL,
        older_url,
        newest_url,
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-31T00:01:08Z", 1785456068.0),
        ("Thu, 31 Jul 2026 00:01:08 +0000", 1785456068.0),
        (1785456068000, 1785456068.0),
    ],
)
def test_parse_message_timestamp_formats(value, expected):
    assert mail_client._parse_message_timestamp({"receivedAt": value}) == expected


def test_structured_api_empty_code_and_null_mail_is_no_new_otp():
    payload = {
        "ok": True,
        "code": "",
        "mail": None,
        # A six-digit sequence in metadata must never be mistaken for the OTP.
        "email": "owner-654321@example.test",
        "fetched_at": "2026-07-31T07:46:55+08:00",
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=datetime(2026, 7, 31, 7, 46, 50, tzinfo=timezone.utc).timestamp(),
    )

    assert code is None
    assert [url for url, _kwargs in session.calls] == [STRUCTURED_API_URL]


def test_structured_api_accepts_code_from_fresh_nested_mail():
    payload = {
        "ok": True,
        "code": "654321",
        "mail": {
            "subject": "Your verification code",
            "received_at": "2026-07-31T07:46:54+08:00",
        },
        "email": "owner@example.test",
        "fetched_at": "2026-07-31T07:46:55+08:00",
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )
    after_ts = datetime(
        2026,
        7,
        31,
        7,
        46,
        50,
        tzinfo=timezone(timedelta(hours=8)),
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code == "654321"


def test_structured_api_rejects_code_from_stale_nested_mail():
    payload = {
        "ok": True,
        "code": "123456",
        "mail": {
            "subject": "Your verification code",
            "received_at": "2026-07-31T07:45:00+08:00",
        },
        "email": "owner@example.test",
        # fetched_at is request time and must not make an old message fresh.
        "fetched_at": "2026-07-31T07:47:01+08:00",
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )
    after_ts = datetime(
        2026,
        7,
        31,
        7,
        47,
        tzinfo=timezone(timedelta(hours=8)),
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code is None


def test_structured_api_rejects_code_without_mail_timestamp_when_after_ts_is_set():
    payload = {
        "ok": True,
        "code": "234567",
        "mail": {
            "subject": "Your verification code",
        },
        "email": "owner@example.test",
        "fetched_at": "2026-07-31T07:47:01+08:00",
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=datetime(
            2026,
            7,
            31,
            7,
            46,
            50,
            tzinfo=timezone(timedelta(hours=8)),
        ).timestamp(),
    )

    assert code is None


def test_structured_api_accepts_valid_code_without_mail_when_after_ts_is_none():
    payload = {
        "ok": True,
        "code": "345678",
        "mail": None,
        "email": "owner@example.test",
        "fetched_at": "2026-07-31T07:47:01+08:00",
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=None,
    )

    assert code == "345678"


def test_mailcom_hub_accepts_fresh_verification_code():
    payload = {
        "ok": True,
        "email": "owner@example.test",
        "found": True,
        "verification_code": "654321",
        "receivedAt": "2026-08-17T08:44:30+08:00",
        "message": {
            "subject": "ChatGPT verification code",
            "verificationCode": "654321",
            "receivedAt": "2026-08-17T08:44:30+08:00",
        },
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )
    after_ts = datetime(
        2026, 8, 17, 8, 44, 25, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code == "654321"


def test_mailcom_hub_rejects_stale_verification_code():
    payload = {
        "ok": True,
        "email": "owner@example.test",
        "found": True,
        "verification_code": "123456",
        "receivedAt": "2026-08-17T08:40:00+08:00",
        "message": {
            "verificationCode": "123456",
            "receivedAt": "2026-08-17T08:40:00+08:00",
        },
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )
    after_ts = datetime(
        2026, 8, 17, 8, 44, 25, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
        after_ts=after_ts,
    )

    assert code is None


def test_mailcom_hub_empty_mail_is_recognized_without_scanning_metadata():
    payload = {
        "ok": True,
        "email": "owner-654321@example.test",
        "found": False,
        "verification_code": None,
        "receivedAt": None,
        "message": None,
    }
    session = FakeSession(
        {
            STRUCTURED_API_URL: _structured_api_response(payload),
        }
    )

    code = mail_client._fetch_current_code(
        session,
        STRUCTURED_API_URL,
        HEADERS,
    )

    assert code is None
