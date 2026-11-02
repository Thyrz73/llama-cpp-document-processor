"""Keep application profiles in one place, outside the user config mount."""

PROFILE_VERSION = 2
PROFILES = {
    "low": {
        "chunk_tokens": 3000,
        "analysis_tokens": 3072,
        "merge_tokens": 2048,
        "final_tokens": 3072,
    },
    "medium": {
        "chunk_tokens": 4000,
        "analysis_tokens": 3584,
        "merge_tokens": 3072,
        "final_tokens": 4096,
    },
    "high": {
        "chunk_tokens": 5500,
        "analysis_tokens": 4096,
        "merge_tokens": 4096,
        "final_tokens": 6144,
    },
    "ultra": {
        "chunk_tokens": 7000,
        "analysis_tokens": 6144,
        "merge_tokens": 6144,
        "final_tokens": 8192,
    },
}



def catalog():
    """Return independent public definitions; no profile is hardware-calibrated."""
    return [{"id": name, "experimental": True, "calibrated": False,
             "version": PROFILE_VERSION, "parameters": dict(values),
             "concurrency": 1} for name, values in PROFILES.items()]
