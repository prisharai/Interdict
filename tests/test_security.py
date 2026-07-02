from hashlib import sha256

from engine.security import audit_safe, redact


def test_audit_safe_redacts_sql_and_task_but_keeps_hashes():
    entry = {
        "sql": "SELECT * FROM users WHERE email = 'person@example.com'",
        "stated_task": "look up person@example.com",
        "decision": {
            "effective_sql": "SELECT * FROM users WHERE email = 'person@example.com'"
        },
    }

    safe = audit_safe(entry)

    assert safe["sql"] == "[REDACTED]"
    assert safe["stated_task"] == "[REDACTED]"
    assert safe["decision"]["effective_sql"] == "[REDACTED]"
    assert safe["sql_sha256"] == sha256(entry["sql"].encode("utf-8")).hexdigest()
    assert (
        safe["stated_task_sha256"]
        == sha256(entry["stated_task"].encode("utf-8")).hexdigest()
    )


def test_redact_masks_sensitive_keys_and_secret_patterns():
    value = {
        "operator_token": "super-secret-token",
        "message": "dsn postgresql://postgres:postgres@localhost/db",
    }

    safe = redact(value)

    assert safe["operator_token"] == "[REDACTED]"
    assert "postgres:postgres" not in safe["message"]
