from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from tests.support.builders import make_webhook
from tests.unit.triggers.helpers import hook_request

from treadle.models import SignatureMode
from treadle.triggers.webhook import (
    DELIVERY_HEADERS,
    MIN_SECRET_LENGTH,
    REDACTED_HEADERS,
    TokenBucket,
    delivery_digest,
    signed_message,
    verify_hmac,
    verify_token,
    webhook_payload,
)

# The example in GitHub's "Validating webhook deliveries" documentation.
GITHUB_SECRET = b"It's a Secret to Everybody"
GITHUB_BODY = b"Hello, World!"
GITHUB_HEX = "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
RECEIVED = datetime(2026, 11, 9, 10, 15, 2, 120000, tzinfo=UTC)


def test_the_redacted_headers_delivery_headers_and_secret_minimum() -> None:
    assert sorted(REDACTED_HEADERS) == ["authorization", "cookie", "proxy-authorization"]
    assert DELIVERY_HEADERS[0] == "x-github-delivery"
    assert MIN_SECRET_LENGTH == 16


class TestVerifyHmac:
    @pytest.mark.parametrize(
        "header",
        [
            f"sha256={GITHUB_HEX}",
            GITHUB_HEX,
            f"SHA256={GITHUB_HEX.upper()}",
            f"  sha256={GITHUB_HEX}\t",
        ],
        ids=["github", "bare-hex", "uppercase", "whitespace"],
    )
    def test_the_documented_github_example_verifies(self, header: str) -> None:
        assert verify_hmac(GITHUB_SECRET, GITHUB_BODY, header)

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "sha256=",
            f"sha256={GITHUB_HEX[:-1]}",
            f"sha256={GITHUB_HEX}0",
            f"sha1={GITHUB_HEX}",
            f"sha256={GITHUB_HEX[:-1]}g",
            f"sha256={GITHUB_HEX[:32]} {GITHUB_HEX[32:]}",
            "sha256=" + "é" * 64,
            "sha256=" + "\uff11" * 64,
            f"sha256={GITHUB_HEX[:-2]}ff",
        ],
        ids=[
            "missing",
            "empty",
            "prefix-only",
            "too-short",
            "too-long",
            "wrong-prefix",
            "not-hex",
            "inner-space",
            "non-ascii",
            "fullwidth-digits",
            "wrong-digest",
        ],
    )
    def test_anything_else_fails_without_raising(self, header: str | None) -> None:
        assert not verify_hmac(GITHUB_SECRET, GITHUB_BODY, header)

    def test_a_different_secret_or_body_fails(self) -> None:
        header = f"sha256={GITHUB_HEX}"
        assert not verify_hmac(b"another secret", GITHUB_BODY, header)
        assert not verify_hmac(GITHUB_SECRET, b"Hello, World?", header)


class TestVerifyToken:
    def test_an_equal_token_verifies(self) -> None:
        assert verify_token(b"gitlab-token-1234", "gitlab-token-1234")

    @pytest.mark.parametrize("header", [None, "", "gitlab-token-123", "gitlab-token-12345"])
    def test_any_other_value_fails(self, header: str | None) -> None:
        assert not verify_token(b"gitlab-token-1234", header)

    def test_a_non_ascii_token_compares_the_bytes_the_sender_sent(self) -> None:
        secret = "tökén-secret-value"
        as_received = secret.encode().decode("latin-1")
        assert verify_token(secret.encode(), as_received)

    def test_characters_beyond_latin_1_fail_without_raising(self) -> None:
        assert not verify_token(b"token-secret-value", "token-secret-€")
        assert verify_token("€-token-secret".encode(), "€-token-secret")


class TestSignedMessage:
    def test_without_a_timestamp_header_the_message_is_the_body(self) -> None:
        trigger = make_webhook("hook")
        assert signed_message(trigger, {}, b"body", now=0.0) == b"body"

    def test_a_timestamp_inside_the_tolerance_prefixes_the_body(self) -> None:
        trigger = make_webhook("hook", timestamp_header="X-Timestamp", tolerance=300)
        headers = {"x-timestamp": " 1700000000 "}
        assert signed_message(trigger, headers, b"body", now=1700000299.0) == b"1700000000.body"
        assert signed_message(trigger, headers, b"body", now=1699999700.0) == b"1700000000.body"

    @pytest.mark.parametrize(
        ("value", "now"),
        [
            (None, 1700000000.0),
            ("", 1700000000.0),
            ("abc", 1700000000.0),
            ("1700000000.5", 1700000000.0),
            ("-1700000000", 1700000000.0),
            ("9" * 40, 1700000000.0),
            ("1700000000", 1700000300.5),
            ("1700000000", 1699999699.0),
        ],
        ids=[
            "missing",
            "empty",
            "letters",
            "fraction",
            "negative",
            "huge",
            "too-old",
            "too-new",
        ],
    )
    def test_a_bad_or_stale_timestamp_gives_no_message(self, value: str | None, now: float) -> None:
        trigger = make_webhook("hook", timestamp_header="X-Timestamp", tolerance=300)
        headers = {} if value is None else {"x-timestamp": value}
        assert signed_message(trigger, headers, b"body", now=now) is None


class TestDeliveryDigest:
    def test_the_digest_covers_the_hook_name_and_the_message(self) -> None:
        digest = delivery_digest(make_webhook("blog-push"), b'{"ref":"main"}')
        assert digest == "a288ce805952416155873439ff99ab9c520fca07756857106ae9c8db7077b8c9"
        expected = hashlib.sha256(b'blog-push\0{"ref":"main"}').hexdigest()
        assert digest == expected

    def test_other_hooks_and_messages_get_other_digests(self) -> None:
        digests = {
            delivery_digest(make_webhook("a"), b"1.body"),
            delivery_digest(make_webhook("b"), b"1.body"),
            delivery_digest(make_webhook("a"), b"2.body"),
        }
        assert len(digests) == 3


class TestPayload:
    def test_the_payload_has_the_documented_shape(self) -> None:
        trigger = make_webhook("blog-push", signature_header="X-Gitea-Signature")
        body = b'{"ref": "refs/heads/main", "after": "6dcb09b5"}'
        request = hook_request(
            body,
            headers={
                "x-gitea-event": "push",
                "x-gitea-signature": "abc",
                "x-gitea-delivery": "f2b1c0de",
                "authorization": "Bearer x",
                "cookie": "a=b",
                "proxy-authorization": "Basic y",
            },
            query={"source": "ci"},
        )
        assert webhook_payload(trigger, request, received_at=RECEIVED) == {
            "kind": "webhook",
            "hook": "blog-push",
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "x-gitea-event": "push",
                "x-gitea-delivery": "f2b1c0de",
            },
            "query": {"source": "ci"},
            "content_type": "application/json",
            "body": {"ref": "refs/heads/main", "after": "6dcb09b5"},
            "delivery": "f2b1c0de",
            "received_at": "2026-11-09T10:15:02.120Z",
        }

    def test_the_token_header_is_left_out_too(self) -> None:
        trigger = make_webhook("hook", signature=SignatureMode.TOKEN)
        request = hook_request(headers={"x-gitlab-token": "secret", "x-gitlab-event": "Push"})
        headers = webhook_payload(trigger, request, received_at=RECEIVED)["headers"]
        assert headers == {"content-type": "application/json", "x-gitlab-event": "Push"}

    def test_header_names_are_lowercased_before_filtering(self) -> None:
        trigger = make_webhook("hook")
        request = hook_request(headers={"Authorization": "x", "X-Hub-Signature-256": "y"})
        headers = webhook_payload(trigger, request, received_at=RECEIVED)["headers"]
        assert headers == {"content-type": "application/json"}

    def test_the_first_present_delivery_header_wins(self) -> None:
        trigger = make_webhook("hook")
        request = hook_request(
            headers={"x-gitlab-event-uuid": "gitlab", "x-forgejo-delivery": "forgejo"}
        )
        payload = webhook_payload(trigger, request, received_at=RECEIVED)
        assert payload["delivery"] == "forgejo"

    def test_without_a_delivery_header_delivery_is_null(self) -> None:
        payload = webhook_payload(make_webhook("hook"), hook_request(), received_at=RECEIVED)
        assert payload["delivery"] is None

    @pytest.mark.parametrize(
        "content_type", ["application/json", "application/vnd.github+json", "text/x+json"]
    )
    def test_a_json_content_type_parses_the_body(self, content_type: str) -> None:
        request = hook_request(b'[1, 2.5, {"a": null}]', content_type=content_type)
        payload = webhook_payload(make_webhook("hook"), request, received_at=RECEIVED)
        assert payload["body"] == [1, 2.5, {"a": None}]

    @pytest.mark.parametrize("content_type", ["text/plain", "+json", None])
    def test_any_other_body_is_text_with_replacements(self, content_type: str | None) -> None:
        request = hook_request(b"caf\xc3\xa9 \xff", content_type=content_type)
        payload = webhook_payload(make_webhook("hook"), request, received_at=RECEIVED)
        assert payload["body"] == "café �"

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"{", "Expecting"),
            (b"", "Expecting value"),
            (b"[NaN]", "NaN isn't valid JSON"),
            (b"[-Infinity]", "-Infinity isn't valid JSON"),
            (b"[1e999]", "out of range"),
            (b'"caf\xff"', "can't decode"),
            (b"[" * 65 + b"]" * 65, "deeper than 64 levels"),
            (b"[" * 100000 + b"]" * 100000, "deeper than 64 levels"),
        ],
        ids=["syntax", "empty", "nan", "infinity", "overflow", "utf8", "nesting", "recursion"],
    )
    def test_an_invalid_json_body_raises_value_error(self, body: bytes, message: str) -> None:
        request = hook_request(body)
        with pytest.raises(ValueError, match=message):
            webhook_payload(make_webhook("hook"), request, received_at=RECEIVED)

    def test_nesting_of_exactly_sixty_four_levels_is_allowed(self) -> None:
        body = b'{"a": ' * 63 + b"[]" + b"}" * 63
        request = hook_request(body)
        payload = webhook_payload(make_webhook("hook"), request, received_at=RECEIVED)
        assert payload["body"] == json.loads(body)


class TestTokenBucket:
    def test_the_burst_is_a_sixth_of_the_rate(self) -> None:
        bucket = TokenBucket(60)
        assert [bucket.take(0.0) for _ in range(10)] == [0.0] * 10
        assert bucket.take(0.0) == pytest.approx(1.0)

    def test_tokens_refill_at_the_rate_per_second(self) -> None:
        bucket = TokenBucket(60)
        for _ in range(10):
            bucket.take(0.0)
        assert bucket.take(0.25) == pytest.approx(0.75)
        assert bucket.take(1.0) == 0.0
        assert bucket.take(1.0) == pytest.approx(1.0)

    def test_refills_never_exceed_the_burst(self) -> None:
        bucket = TokenBucket(60)
        bucket.take(0.0)
        allowed = [bucket.take(3600.0) for _ in range(11)]
        assert allowed.count(0.0) == 10

    def test_a_small_rate_still_allows_one_request(self) -> None:
        bucket = TokenBucket(1)
        assert bucket.take(0.0) == 0.0
        assert bucket.take(0.0) == pytest.approx(60.0)

    def test_zero_disables_the_limit(self) -> None:
        bucket = TokenBucket(0)
        assert all(bucket.take(0.0) == 0.0 for _ in range(1000))

    def test_a_clock_that_goes_back_adds_no_tokens(self) -> None:
        bucket = TokenBucket(6)
        assert bucket.take(100.0) == 0.0
        assert bucket.take(50.0) == pytest.approx(10.0)
