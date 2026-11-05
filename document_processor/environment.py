"""Read deployment settings from the environment, naming the variable on error."""

import os
from urllib.parse import urlsplit

TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")


class SettingsError(ValueError):
    """Report an invalid environment variable without echoing secrets."""


def _raw(name):
    value = os.getenv(name, "").strip()
    return value or None


def integer(name, default, *, minimum=1, maximum=None):
    """Return an integer setting within bounds; empty or unset uses default."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer.") from None
    if value < minimum or (maximum is not None and value > maximum):
        bounds = (
            f"between {minimum} and {maximum}"
            if maximum is not None
            else f"at least {minimum}"
        )
        raise SettingsError(f"{name} must be {bounds}.")
    return value


def flag(name, default):
    """Return a boolean setting; reject values that are neither true nor false."""
    raw = _raw(name)
    if raw is None:
        return default
    if raw.lower() in TRUE_VALUES:
        return True
    if raw.lower() in FALSE_VALUES:
        return False
    raise SettingsError(
        f"{name} must be one of: {', '.join(TRUE_VALUES + FALSE_VALUES)}."
    )


def choice(name, default, allowed):
    """Return one of the allowed values; empty or unset uses default."""
    value = _raw(name) or default
    if value not in allowed:
        raise SettingsError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def http_url(name, default):
    """Return an http(s) base URL without a trailing slash."""
    value = (_raw(name) or default).rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        # The value may embed credentials: never include it in the message.
        raise SettingsError(f"{name} must be an http:// or https:// URL.")
    return value
