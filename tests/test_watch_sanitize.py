"""Watcher-script output sanitizer.

Feature: text printed by a watcher script is untrusted (a CI log can contain tokens), so it is ANSI-stripped and
secret-redacted before it can reach a model prompt. Guarantees: escapes removed, token-like strings replaced
with [REDACTED], ordinary text untouched.
"""
from watch_sanitize import sanitize


def test_ansi_is_stripped_and_secrets_are_redacted_but_plain_text_survives():
    raw = "\x1b[31mERROR\x1b[0m token=glpat-abcdefghij1234567890 AKIAABCDEFGHIJKLMNOP\nAuthorization: Bearer abc.def.ghijklmnopqrstu"
    out = sanitize(raw)
    assert "\x1b" not in out and "glpat-" not in out and "AKIAABC" not in out and "abc.def" not in out
    assert "[REDACTED]" in out and "ERROR" in out


def test_json_cookie_basic_jwt_and_sk_secrets_are_redacted_but_ordinary_json_survives():
    """Feature: besides key=value and Bearer tokens, quoted JSON secrets, Cookie/Set-Cookie headers,
    Authorization Basic credentials, bare JWTs and sk- API keys are redacted; innocuous fields stay."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123_-"
    raw = ('{"access_token": "abc123SECRET", "token_count": "5", "name": "build", "n": 3}\n'
           "Cookie: session=abcd1234; other=zz\nSet-Cookie: sid=xyz; HttpOnly\n"
           "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==\n"
           f"jwt {jwt} key sk-abcdefghijklmnopqrstuvwx1234")
    out = sanitize(raw)
    for leaked in ("abc123SECRET", "abcd1234", "xyz; HttpOnly", "dXNlcjpwYXNz", jwt, "sk-abcdefghij"):
        assert leaked not in out
    assert '"token_count": "5"' in out and '"name": "build"' in out and '"n": 3' in out


def test_ordinary_log_lines_are_not_mangled():
    line = "2025-01-02 build ok: 12 tests passed, basic auth disabled, tokenizer v2, cookie jar empty"
    assert sanitize(line) == line
