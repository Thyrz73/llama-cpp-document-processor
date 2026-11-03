"""Public processing failures shared by extraction, persistence and generation."""


class ProcessingError(RuntimeError):
    """Carry a stable public code and a safe message, never remote bodies or local paths.

    The worker stores both on the failed job; the browser translates the code
    and falls back to the message.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
