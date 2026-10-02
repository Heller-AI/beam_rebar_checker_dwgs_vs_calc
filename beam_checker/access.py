"""Access-code sign-in, lockout and AI call budgets for the shared (owner's) API keys.

No Streamlit code here: `state` is any dict-like object (the app passes `st.session_state`),
so the logic can be unit tested.
"""

import hashlib
import hmac
import threading
import time
from datetime import date

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 10 * 60

_GRANTED = "access_granted_for"   # digest of the code that was accepted
_FAILS = "access_failed_attempts"
_LOCKED_UNTIL = "access_locked_until"


class BudgetExceeded(Exception):
    """A call limit was reached; the message is safe to show to the user."""


def _digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def is_signed_in(state, password):
    """True if this session already entered the current code (a changed code signs everyone out)."""
    return bool(password) and state.get(_GRANTED) == _digest(password)


def sign_out(state):
    state.pop(_GRANTED, None)


def lock_remaining_seconds(state, now=None):
    now = time.time() if now is None else now
    return max(0, int(state.get(_LOCKED_UNTIL, 0) - now))


def check_access_code(state, entered, password, now=None):
    """Check a submitted code. Returns "ok", "wrong", "locked" or "empty".

    After MAX_FAILED_ATTEMPTS wrong codes the session is locked for LOCKOUT_SECONDS;
    attempts during the lock are not checked at all.
    """
    now = time.time() if now is None else now
    if not password:
        return "wrong"
    if lock_remaining_seconds(state, now):
        return "locked"
    if state.get(_LOCKED_UNTIL):  # lock expired: start counting again
        state[_LOCKED_UNTIL] = 0
        state[_FAILS] = 0
    if not entered:
        return "empty"

    if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
        state[_GRANTED] = _digest(password)
        state[_FAILS] = 0
        return "ok"

    state[_FAILS] = state.get(_FAILS, 0) + 1
    if state[_FAILS] >= MAX_FAILED_ATTEMPTS:
        state[_LOCKED_UNTIL] = now + LOCKOUT_SECONDS
        return "locked"
    return "wrong"


def attempts_left(state):
    return max(0, MAX_FAILED_ATTEMPTS - state.get(_FAILS, 0))


# ------------------------------------------------------------------ call budgets

def session_calls_used(state, bucket):
    return state.get(f"ai_calls_{bucket}", 0)


class DailyCounter:
    """Server-wide count of AI calls per calendar day, shared by all sessions.

    In memory only: it resets when the app restarts (e.g. a Streamlit Cloud reboot or redeploy).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._day = None
        self._count = 0

    def used(self, today=None):
        today = today or date.today()
        with self._lock:
            return self._count if self._day == today else 0

    def try_consume(self, limit, today=None):
        today = today or date.today()
        with self._lock:
            if self._day != today:
                self._day, self._count = today, 0
            if self._count >= limit:
                return False
            self._count += 1
            return True


def check_call(state, bucket, session_limit, daily_counter, daily_limit, today=None):
    """Raise BudgetExceeded if one more model request would pass a limit; counts nothing.

    Used before a request is sent; consume_call counts it once a response has arrived, so a request that
    fails (network or API error) is not charged to the allowance.
    """
    if state.get(f"ai_calls_{bucket}", 0) >= session_limit:
        raise BudgetExceeded(
            f"This session has used its {session_limit} AI calls on the shared key. "
            "Ask the app owner for more, or use your own API key."
        )
    if daily_counter.used(today) >= daily_limit:
        raise BudgetExceeded(
            "The shared key's daily AI limit for this app has been reached. "
            "Try again tomorrow, or use your own API key."
        )


def count_call(state, bucket, daily_counter, today=None):
    """Count one model request that has returned a response (after check_call allowed it). Never raises."""
    state[f"ai_calls_{bucket}"] = state.get(f"ai_calls_{bucket}", 0) + 1
    daily_counter.try_consume(float("inf"), today)


def consume_call(state, bucket, session_limit, daily_counter, daily_limit, today=None):
    """Count one model request against the session cap and the server-wide daily cap.

    Raises BudgetExceeded (without consuming anything) if either limit is reached.
    """
    key = f"ai_calls_{bucket}"
    if state.get(key, 0) >= session_limit:
        raise BudgetExceeded(
            f"This session has used its {session_limit} AI calls on the shared key. "
            "Ask the app owner for more, or use your own API key."
        )
    if not daily_counter.try_consume(daily_limit, today):
        raise BudgetExceeded(
            "The shared key's daily AI limit for this app has been reached. "
            "Try again tomorrow, or use your own API key."
        )
    state[key] = state.get(key, 0) + 1


def redact(text, secrets):
    """Replace any secret value that appears in `text` with [hidden]."""
    text = str(text)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[hidden]")
    return text
