"""Small deterministic doubles; no app import, environment setup or shared state.

Suites retain their own model identities, completion text, failures and call
records. Encoding tests keep their real Requests adapter and byte fragmentation.
"""

import json


class WordCountModelMixin:
    """Common identity refresh and token counting for pipeline model doubles."""

    def inspect_identity(self):
        return self.inspect()

    def tokenize(self, text):
        self.tokenization_calls += 1
        return len(text.split())


def sse_event(delta, finish=None):
    """One chat-completion delta, without assuming a delta is one token."""
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


class SSEStream:
    """HTTP response double emitting JSON events or raw lines, optionally [DONE]."""

    def __init__(self, events, done=True):
        self.events, self.done = events, done

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield (
                event
                if isinstance(event, bytes)
                else ("data: " + json.dumps(event)).encode()
            )
        if self.done:
            yield b"data: [DONE]"
