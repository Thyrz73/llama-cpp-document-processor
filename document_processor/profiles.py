"""Performance profiles: the token budgets a new job can choose in the interface."""

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
    """Return the profiles as published by /api/profiles; none is calibrated for a given machine."""
    return [{"id": name, "experimental": True, "calibrated": False,
             "version": PROFILE_VERSION, "parameters": dict(values),
             "concurrency": 1} for name, values in PROFILES.items()]
