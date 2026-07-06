"""Security regression tests."""

from custom_components.volvooncall_cn.volvooncall_base import redact_sensitive


def test_redact_sensitive_removes_tokens_from_log_strings():
    raw = (
        "url='https://apigateway.digitalvolvo.com/app/iam/api/v1/"
        "refreshToken?refreshToken=secret-refresh-token' "
        "authorization: Bearer secret-access-token "
        "X-Token: secret-x-token"
    )

    redacted = redact_sensitive(raw)

    assert "secret-refresh-token" not in redacted
    assert "secret-access-token" not in redacted
    assert "secret-x-token" not in redacted
    assert "refreshToken=<redacted>" in redacted
    assert "Bearer <redacted>" in redacted
    assert "X-Token: <redacted>" in redacted
