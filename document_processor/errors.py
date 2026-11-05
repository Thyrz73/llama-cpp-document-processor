"""Public processing failures shared by extraction, persistence and generation."""


class ProcessingError(RuntimeError):
    """Carry a stable public code and a safe message, never remote bodies or local paths.

    The worker stores both on the failed job; the browser translates the code
    and falls back to the message.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ModelError(ProcessingError):
    """Report a model backend or generation failure without remote response bodies."""


class Cancelled(ProcessingError):
    """Signal cooperative cancellation without committing an incomplete artifact.

    Keep it separate from ModelError so optional backend fallbacks cannot
    turn cancellation into an unavailable capability or a generation failure.
    """

    def __init__(self):
        super().__init__("cancelled", "Analysis cancelled.")
