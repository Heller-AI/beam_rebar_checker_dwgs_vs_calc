from datetime import date

import pytest

from beam_checker import access

PW = "correct-horse-battery"


def test_correct_code_signs_in_and_is_remembered():
    state = {}
    assert not access.is_signed_in(state, PW)
    assert access.check_access_code(state, PW, PW) == "ok"
    assert access.is_signed_in(state, PW)
    access.sign_out(state)
    assert not access.is_signed_in(state, PW)


def test_changing_the_code_signs_everyone_out():
    state = {}
    access.check_access_code(state, PW, PW)
    assert not access.is_signed_in(state, "new-code")


def test_empty_code_is_not_counted_as_a_failure():
    state = {}
    assert access.check_access_code(state, "", PW) == "empty"
    assert access.attempts_left(state) == access.MAX_FAILED_ATTEMPTS


def test_lockout_after_five_wrong_codes_then_unlock_after_ten_minutes():
    state, t0 = {}, 1_000_000.0
    for i in range(access.MAX_FAILED_ATTEMPTS - 1):
        assert access.check_access_code(state, "nope", PW, now=t0 + i) == "wrong"
    assert access.check_access_code(state, "nope", PW, now=t0 + 10) == "locked"

    # While locked even the right code is refused
    assert access.check_access_code(state, PW, PW, now=t0 + 60) == "locked"
    assert not access.is_signed_in(state, PW)
    assert 0 < access.lock_remaining_seconds(state, now=t0 + 60) <= access.LOCKOUT_SECONDS

    # After the lock expires the counter restarts and the right code works
    later = t0 + 10 + access.LOCKOUT_SECONDS + 1
    assert access.lock_remaining_seconds(state, now=later) == 0
    assert access.check_access_code(state, PW, PW, now=later) == "ok"
    assert access.attempts_left(state) == access.MAX_FAILED_ATTEMPTS


def test_no_password_configured_never_grants():
    state = {}
    assert access.check_access_code(state, "", "") == "wrong"
    assert not access.is_signed_in(state, "")


def test_session_cap():
    state, counter = {}, access.DailyCounter()
    for _ in range(3):
        access.consume_call(state, "chat", 3, counter, 100)
    with pytest.raises(access.BudgetExceeded, match="3 AI calls"):
        access.consume_call(state, "chat", 3, counter, 100)
    assert access.session_calls_used(state, "chat") == 3
    # Separate buckets have separate session caps
    access.consume_call(state, "drawing", 3, counter, 100)
    assert access.session_calls_used(state, "drawing") == 1


def test_daily_cap_is_shared_between_sessions_and_resets_next_day():
    counter, day1, day2 = access.DailyCounter(), date(2026, 1, 1), date(2026, 1, 2)
    a, b = {}, {}
    access.consume_call(a, "chat", 10, counter, 2, today=day1)
    access.consume_call(b, "chat", 10, counter, 2, today=day1)
    with pytest.raises(access.BudgetExceeded, match="daily"):
        access.consume_call(a, "chat", 10, counter, 2, today=day1)
    assert access.session_calls_used(a, "chat") == 1  # refused call is not counted
    access.consume_call(a, "chat", 10, counter, 2, today=day2)
    assert counter.used(today=day2) == 1


def test_redact_hides_keys_and_codes():
    text = "Error with key sk-ant-SECRET123 and code " + PW
    out = access.redact(text, ["sk-ant-SECRET123", PW, "", None])
    assert "SECRET" not in out and PW not in out and out.count("[hidden]") == 2
