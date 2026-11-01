"""Keep application profiles in one place, outside the user config mount."""

PROFILE_VERSION = 1
PROFILES = {
    "low": {"chunk_tokens": 3000, "analysis_tokens": 1000, "merge_tokens": 1200},
    "medium": {"chunk_tokens": 3500, "analysis_tokens": 1100, "merge_tokens": 1300},
    "high": {"chunk_tokens": 4000, "analysis_tokens": 1200, "merge_tokens": 1400},
    "ultra": {"chunk_tokens": 4500, "analysis_tokens": 1400, "merge_tokens": 1600},
}


def catalog():
    """Return independent public definitions; no profile is hardware-calibrated."""
    return [{"id": name, "experimental": True, "calibrated": False,
             "version": PROFILE_VERSION, "parameters": dict(values),
             "concurrency": 1} for name, values in PROFILES.items()]
