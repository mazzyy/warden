"""The audit trail records what came back, not just that a call was allowed.

Before this, the log said `diagnostician · get_workload_logs · allow · 229ms`
and stopped there. That proves a call was permitted. It does not let anyone
check a diagnosis against the evidence it was built from — which, for an audit
trail whose subject is an agent's reasoning, is most of the point. A confident
root cause you cannot trace back to a log line is exactly the class of claim
this project keeps catching.

Recording results creates a second problem, though, and it is the reason for
the scrubbing tests below: tool results here are cluster reads, and a pod spec
carries its container's whole environment. Store that unredacted and the audit
log becomes the most readable secret store in the system.
"""

from __future__ import annotations

import json

from warden.proxy.plugin import WardenPolicyPlugin as P

# --------------------------------------------------------------------------
# What gets recorded
# --------------------------------------------------------------------------


def test_a_result_is_rendered_as_readable_json():
    preview, truncated = P._preview({"alert": {"signature": "checkout-svc/CrashLoopBackOff"}})
    assert "checkout-svc/CrashLoopBackOff" in preview
    assert "\n" in preview, "should be indented, it is read by a human"
    assert truncated is False
    json.loads(preview)


def test_no_result_records_nothing_rather_than_the_string_none():
    assert P._preview(None) == ("", False)


def test_an_unserialisable_result_still_records_something():
    """A demo must not die because a tool returned an odd object."""

    class Odd:
        def __repr__(self):
            return "<Odd>"

    preview, _ = P._preview({"thing": Odd()})
    assert "Odd" in preview


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


def test_a_huge_result_is_truncated_and_says_so():
    preview, truncated = P._preview({"logs": ["a line of pod output"] * 5000})
    assert truncated is True
    assert len(preview) <= P.MAX_RESULT_CHARS + 40
    assert preview.endswith("… truncated")


def test_the_cap_is_big_enough_to_be_useful():
    """Small enough to stay a log, big enough to hold a real log excerpt."""
    assert 1000 <= P.MAX_RESULT_CHARS <= 20000


# --------------------------------------------------------------------------
# Redaction — the reason this needed care
# --------------------------------------------------------------------------


def test_a_secret_looking_key_is_redacted():
    preview, _ = P._preview({"env": {"DB_PASSWORD": "hunter2", "LOG_LEVEL": "info"}})
    assert "hunter2" not in preview
    assert "«redacted»" in preview
    assert "info" in preview, "redaction must not swallow the whole result"


def test_redaction_reaches_into_nested_structures():
    """A pod spec is containers inside a list inside a dict. Depth matters."""
    result = {
        "containers": [
            {"name": "checkout", "env": {"API_KEY": "sk-live-xxxx", "PORT": "8080"}},
        ]
    }
    preview, _ = P._preview(result)
    assert "sk-live-xxxx" not in preview
    assert "8080" in preview


def test_every_documented_secret_hint_is_actually_redacted():
    for hint in P.SECRET_HINTS:
        preview, _ = P._preview({f"MY_{hint.upper()}": "leak-me"})
        assert "leak-me" not in preview, f"{hint} was not redacted"


def test_matching_survives_every_spelling_of_the_same_key():
    """The first version listed spellings and missed `x-api-key`. Keys are now
    normalised — punctuation and case stripped — so one hint covers them all."""
    for key in ("Password", "GITHUB_TOKEN", "clientSecret", "x-api-key", "API KEY", "apiKey"):
        preview, _ = P._preview({key: "leak-me"})
        assert "leak-me" not in preview, key


def test_ordinary_config_survives_untouched():
    """A redactor that eats everything is as useless as one that eats nothing."""
    preview, _ = P._preview(
        {"env": {"PAYMENT_ENDPOINT": "htps://payments.internal/v2", "TIMEOUT_MS": "3000"}}
    )
    assert "htps://payments.internal/v2" in preview, "the actual bug must stay visible"
    assert "3000" in preview
