"""Talk to the llama.cpp server: inspect it, count tokens and stream generations.

Only the server set by the administrator is contacted. Streamed text is split
into the answer and the model's reasoning, and live previews keep only a
bounded tail of each.
"""

import copy
import hashlib
import json
import math
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import requests

from .config import ConfigurationError
from .contracts import ChatMessage, CompletionMetrics, JobConfig
from .errors import Cancelled, ModelError, WorkerStopping


@dataclass
class CompletionResult:
    """The answer of one generation and its measurements, which contain no text."""

    content: str
    metrics: CompletionMetrics


def numeric(value):
    """Return value if it is a finite, non-negative number, else None."""
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def collect_metrics(event, metrics):
    """Copy the token counts and timings reported in one stream event into metrics.

    Only values sent by the engine are recorded; rates are derived from them.
    """
    usage = event.get("usage") or {}
    timings = event.get("timings") or {}
    if not isinstance(usage, dict) or not isinstance(timings, dict):
        return
    mapping = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "evaluated_prompt_tokens": timings.get("prompt_n"),
        "prefill_ms": timings.get("prompt_ms"),
        "decode_ms": timings.get("predicted_ms"),
    }
    if mapping["output_tokens"] is None:
        mapping["output_tokens"] = timings.get("predicted_n")
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        mapping["reasoning_tokens"] = details.get("reasoning_tokens")
    if isinstance(prompt_details, dict):
        mapping["cached_prompt_tokens"] = prompt_details.get("cached_tokens")
    for key, value in mapping.items():
        if numeric(value) is not None:
            metrics[key] = value
            metrics[key + "_source"] = "engine"
    for name, tokens, elapsed in (
        ("prefill_tokens_per_second", "evaluated_prompt_tokens", "prefill_ms"),
        ("decode_tokens_per_second", "output_tokens", "decode_ms"),
    ):
        if metrics.get(tokens) is not None and metrics.get(elapsed):
            metrics[name] = metrics[tokens] * 1000 / metrics[elapsed]
    # Derive the answer token count only when the engine's figures are consistent.
    if (
        metrics.get("reasoning_tokens") is not None
        and metrics.get("output_tokens") is not None
        and metrics["reasoning_tokens"] <= metrics["output_tokens"]
    ):
        metrics["useful_tokens"] = (
            metrics["output_tokens"] - metrics["reasoning_tokens"]
        )
        metrics["useful_tokens_source"] = "engine_derived"


class ThinkingSplitter:
    """Separate <think>...</think> reasoning from the answer in streamed text.

    A tag may be split across stream events. initial_thinking starts inside a
    reasoning block, for templates that open it themselves; reasoning_limit
    keeps only the latest reasoning characters.
    """

    def __init__(self, *, initial_thinking=False, reasoning_limit=None):
        self.content_parts, self.reasoning_parts = [], []
        self.pending = ""
        self.inside_think = initial_thinking
        self.reasoning_limit = reasoning_limit
        self.content_chars = 0
        self.reasoning_chars = 0

    @property
    def content(self):
        """The answer text received so far."""
        return "".join(self.content_parts)

    @property
    def reasoning(self):
        """The reasoning text kept so far: its latest reasoning_limit characters, if set."""
        return "".join(self.reasoning_parts)

    def append(self, text):
        """Count emitted characters once, including pending text flushed by finish()."""
        if self.inside_think:
            self.reasoning_chars += len(text)
            self.reasoning_parts.append(text)
            if self.reasoning_limit is not None:
                self.reasoning_parts = (
                    [self.reasoning[-self.reasoning_limit :]]
                    if self.reasoning_limit
                    else []
                )
        else:
            self.content_chars += len(text)
            self.content_parts.append(text)

    def feed(self, fragment):
        """Add a streamed fragment, holding back a possibly incomplete tag until the next one."""
        self.pending += fragment
        while self.pending:
            marker = "</think>" if self.inside_think else "<think>"
            pos = self.pending.find(marker)
            if pos >= 0:
                self.append(self.pending[:pos])
                self.pending = self.pending[pos + len(marker) :]
                self.inside_think = not self.inside_think
                continue
            keep = 0
            for size in range(1, min(len(marker), len(self.pending) + 1)):
                if self.pending.endswith(marker[:size]):
                    keep = size
            self.append(self.pending[: len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep :]
            break

    def finish(self):
        """Add the text held back by feed() once the stream has ended."""
        self.append(self.pending)
        self.pending = ""


@dataclass(frozen=True)
class StreamState:
    """A preview of the parsed channels, with the generation's shared metrics.

    completed means finish() validated a nonempty, untruncated answer. Receiving
    [DONE] alone does not make a partial answer safe to publish as final.
    """

    content: str
    reasoning: str
    metrics: CompletionMetrics
    completed: bool


class StreamParser:
    """Parse complete SSE lines without HTTP, threads or preview scheduling.

    The transport assembles fragmented wire bytes into lines. feed() decodes
    UTF-8 and JSON, collects deltas and measurements, and reports whether a
    JSON event was consumed. finish() validates completion after [DONE] or EOF.
    Text is assembled only for state snapshots and the final answer; counters
    remain incremental so small deltas do not repeatedly scan the whole answer.
    """

    def __init__(
        self,
        *,
        preview_limit: int = 12000,
        initial_thinking: bool = False,
        thinking_policy: object = "inherit",
        started: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock = time.monotonic if clock is None else clock
        self._started = self._clock() if started is None else started
        self._preview_limit = preview_limit
        self._initial_thinking = initial_thinking
        self._splitter = ThinkingSplitter(
            initial_thinking=initial_thinking and thinking_policy != "off",
            reasoning_limit=preview_limit,
        )
        self._reasoning = ""
        self._reasoning_chars = 0
        self._finish_reason: object = None
        self.done = False
        self.completed = False
        self.metrics: dict[str, Any] = {
            key: None
            for key in (
                "prompt_tokens",
                "output_tokens",
                "reasoning_tokens",
                "useful_tokens",
                "cached_prompt_tokens",
                "evaluated_prompt_tokens",
                "prefill_ms",
                "decode_ms",
                "first_event_ms",
                "first_content_ms",
                "finish_reason",
            )
        }

    def feed(self, line: bytes | str | None) -> bool:
        """Consume one transport line; stop accepting events after [DONE]."""
        if not line or self.done or self.completed:
            return False
        if not isinstance(line, str):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                raise ModelError(
                    "invalid_stream_encoding", "The model stream is not valid UTF-8."
                ) from None
        if not line.startswith("data:"):
            return False
        message = line[5:].strip()
        if message == "[DONE]":
            self.done = True
            return False
        try:
            event = json.loads(message)
        except ValueError:
            raise ModelError(
                "invalid_stream", "Malformed llama.cpp streaming response."
            ) from None
        self._event(event)
        self._check_size()
        return True

    def _event(self, event: object) -> None:
        if not isinstance(event, dict) or "error" in event:
            raise ModelError("stream_error", "The backend reported a streaming error.")
        if self.metrics["first_event_ms"] is None:
            self.metrics["first_event_ms"] = (self._clock() - self._started) * 1000
        collect_metrics(event, self.metrics)
        choices = event.get("choices") or []
        if not isinstance(choices, list):
            raise ModelError("invalid_stream", "Malformed streaming choices.")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ModelError("invalid_stream", "Malformed streaming choice.")
            self._choice(choice)

    def _choice(self, choice: dict[str, Any]) -> None:
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise ModelError("invalid_stream", "Malformed streaming delta.")
        part = delta.get("reasoning_content") or delta.get("reasoning")
        # Server-separated reasoning has already applied the template's parser;
        # do not classify its useful content a second time, even with policy off.
        if isinstance(part, str) and part and self._initial_thinking:
            self._splitter.inside_think = False
        if isinstance(delta.get("content"), str):
            self._splitter.feed(delta["content"])
            if (
                self._splitter.content_parts
                and self.metrics["first_content_ms"] is None
            ):
                self.metrics["first_content_ms"] = (
                    self._clock() - self._started
                ) * 1000
        if isinstance(part, str):
            self._reasoning_chars += len(part)
            self._reasoning = (self._reasoning + part)[-self._preview_limit :]
        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]

    def _check_size(self) -> None:
        if self._splitter.content_chars > 4_000_000:
            raise ModelError(
                "output_too_large", "The generated response exceeded the safety limit."
            )

    def finish(self) -> str:
        """Flush pending text and return only a validated final answer."""
        self._splitter.finish()
        # A trailing partial tag is ordinary text once finish() flushes it.
        self._check_size()
        self.metrics["finish_reason"] = (
            self._finish_reason
            if self._finish_reason in ("stop", "length", "content_filter", "tool_calls")
            else None
        )
        if self._finish_reason == "length":
            raise ModelError(
                "output_limit",
                "Model response hit max_tokens or the context limit; start a new job with adjusted budgets.",
            )
        if self._finish_reason not in (None, "stop"):
            raise ModelError(
                "unsupported_finish",
                "The backend returned an unsupported completion reason.",
            )
        if not self.done and self._finish_reason is None:
            raise ModelError(
                "incomplete_stream", "llama.cpp stream closed before completion."
            )
        self.metrics["stop_category"] = (
            "stop" if self._finish_reason == "stop" else "done_without_reason"
        )
        answer = self._splitter.content.strip()
        if not answer:
            raise ModelError(
                "empty_output",
                "Model returned empty content; start a new job with adjusted budgets.",
            )
        self.completed = True
        return answer

    def update_counts(self) -> None:
        """Update measurements without assembling the accumulated text."""
        self.metrics["content_chars"] = self._splitter.content_chars
        self.metrics["reasoning_chars"] = (
            self._reasoning_chars + self._splitter.reasoning_chars
        )

    @property
    def state(self) -> StreamState:
        """Assemble a preview on demand, retaining only the reasoning tail."""
        self.update_counts()
        return StreamState(
            self._splitter.content,
            (self._splitter.reasoning + self._reasoning)[-self._preview_limit :],
            cast(CompletionMetrics, self.metrics),
            self.completed,
        )


# Only one generation streams at a time. A cancelled read may take time to
# close, so the next generation waits until the previous reader has exited,
# even if the server ignores the disconnection.
_stream_gate = threading.Lock()


class ModelClient:
    """Client for the llama.cpp server of the deployment.

    base_url and key come from the deployment settings, never from a job.
    config is the job configuration, prompt its system prompt, and
    preview_limit the number of characters kept for live previews.
    """

    def __init__(
        self,
        base_url: str,
        key: str,
        config: JobConfig,
        prompt: str,
        preview_limit: int = 12000,
    ) -> None:
        self.base_url, self.config, self.prompt = base_url.rstrip("/"), config, prompt
        self.preview_limit = preview_limit
        self.session = requests.Session()
        if key:
            self.session.headers.update({"Authorization": f"Bearer {key}"})
        self.model_name: str | None = None
        self.capabilities: dict[str, Any] = {
            "context_tokens": None,
            "status": "unknown",
        }
        self.token_cache: OrderedDict[bytes, int] = OrderedDict()
        self.tokenization_calls = 0
        self.tokenization_ms = 0.0
        self.tokenization_cache_hits = 0
        self.last_result: CompletionResult | None = None
        self.cancel_check = lambda: False
        self.current_stage = "section"
        self.current_metrics: dict[str, Any] = {}

    def check_cancelled(self):
        """Raise Cancelled when the job's cancel_check() reports a cancellation request."""
        if self.cancel_check():
            raise Cancelled()

    def json_request(self, method, path, *, timeout=90, **kwargs):
        """Send a request to the server and return its decoded JSON body.

        Raise ModelError when the request fails or the body is not JSON. The
        error never includes the response body or the URL, which may contain
        credentials.
        """
        self.check_cancelled()
        try:
            with self.session.request(
                method, self.base_url + path, timeout=(5, timeout), **kwargs
            ) as response:
                response.raise_for_status()
                # JSON from llama.cpp is UTF-8, independently of HTTP charset
                # guesses (or a proxy's misleading Content-Type).
                return json.loads(response.content.decode("utf-8"))
        except (requests.RequestException, ValueError):
            raise ModelError(
                "backend_unavailable",
                "The model backend is unavailable or returned invalid data.",
            ) from None

    def inspect(self, *, cached=None):
        """Return sanitized server properties without generating anything.

        The model comes from /v1/models; the context size, slots, template
        hash, version and default sampling come from the optional /props.
        Unavailable properties are reported in the result; cancellation propagates.
        cached optionally supplies (full model ID, sanitized properties) from
        an unexpired cache. Always recheck the model ID before reusing them.
        """
        return self._inspect(cached=cached)

    def inspect_identity(self):
        """Read the model and properties afresh before an attempt can reuse artifacts.

        Never accept a capability-cache hint here. Model failures propagate
        without a second request; optional properties can remain unknown,
        in which case the artifact compatibility check must refuse reuse.
        """
        return self._inspect(for_identity=True)

    def _inspect(self, *, cached=None, for_identity=False):
        """Collect observations for either diagnostics or attempt identity validation."""
        result: dict[str, Any] = {
            "status": "unavailable",
            "context_tokens": None,
            "slots": None,
            "model": None,
            "template_hash": None,
            "version": None,
            "sampling": {},
            "source": "backend",
            "observed_at": time.time(),
        }
        try:
            self.model_name = None
            result["model"] = self.model().replace("\\", "/").split("/")[-1][:120]
            result["status"] = "available"
        except ModelError:
            self.capabilities = result
            if for_identity:
                raise
            return result
        if (
            cached is not None
            and cached[0] == self.model_name
            and cached[1].get("status") == "available"
        ):
            self.capabilities = copy.deepcopy(cached[1])
            return self.capabilities
        try:
            props = self.json_request("GET", "/props", timeout=5)
            settings = props.get("default_generation_settings") or {}
            context = settings.get("n_ctx") if isinstance(settings, dict) else None
            if type(context) is int and context > 0:
                result["context_tokens"] = context
            parameters = (
                settings.get("params", {}) if isinstance(settings, dict) else {}
            )
            if isinstance(parameters, dict):
                names = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty")
                result["sampling"] = {
                    key: parameters[key]
                    for key in names
                    if numeric(parameters.get(key)) is not None
                }
                # Display may omit invalid values; identity must not hash a partial observation.
                if for_identity and any(
                    key in parameters and numeric(parameters[key]) is None
                    for key in names
                ):
                    result["sampling"] = {}
            slots = props.get("total_slots")
            if type(slots) is int and slots > 0:
                result["slots"] = slots
            template = props.get("chat_template")
            if isinstance(template, str):
                result["template_hash"] = hashlib.sha256(
                    template.encode("utf-8")
                ).hexdigest()
            # Do not serialize arbitrary build metadata: it may contain local paths.
            version = props.get("version")
            if (
                isinstance(version, str)
                and len(version) < 80
                and all(c.isalnum() or c in ".-_ " for c in version)
            ):
                result["version"] = version
        except (ModelError, AttributeError, TypeError):
            result["properties"] = "unsupported_or_unavailable"
        self.capabilities = result
        return result

    def tokenize(self, text: str) -> int:
        """Return the number of tokens in text, counted by the server's /tokenize.

        Counts are cached for this client: at most 1024 entries, keyed by SHA-256.
        """
        self.check_cancelled()
        key = hashlib.sha256(text.encode("utf-8")).digest()
        if key in self.token_cache:
            self.tokenization_cache_hits += 1
            self.token_cache.move_to_end(key)
            return self.token_cache[key]
        started = time.monotonic()
        try:
            payload = self.json_request(
                "POST", "/tokenize", json={"content": text, "add_special": False}
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("tokens"), list
            ):
                raise ModelError(
                    "invalid_tokenizer", "Unexpected response from /tokenize."
                )
            count = len(payload["tokens"])
        finally:
            self.tokenization_calls += 1
            self.tokenization_ms += (time.monotonic() - started) * 1000
        self.token_cache[key] = count
        if len(self.token_cache) > 1024:
            self.token_cache.popitem(last=False)
        return count

    def model(self):
        """Return the ID of the first model listed by /v1/models, remembered afterwards."""
        if self.model_name:
            return self.model_name
        payload = self.json_request("GET", "/v1/models", timeout=5)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if (
            not isinstance(items, list)
            or not items
            or not isinstance(items[0], dict)
            or not isinstance(items[0].get("id"), str)
            or not items[0]["id"].strip()
        ):
            raise ModelError("missing_model", "No model returned by /v1/models.")
        self.model_name = items[0]["id"]
        return self.model_name

    def messages(self, instruction: str, content: str) -> list[ChatMessage]:
        """Build the system and user messages of one generation.

        With the "prompt" reasoning adapter, add /think or /no_think to the
        instruction when the current stage sets a reasoning policy.
        """
        policy = self.config.get("thinking_" + self.current_stage, "inherit")
        if policy != "inherit" and self.config.get("thinking_adapter") == "prompt":
            instruction += "\n" + ("/think" if policy == "on" else "/no_think")
        return [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": instruction + "\n\n" + content},
        ]

    def prompt_size(self, instruction: str, content: str) -> tuple[int, str, int]:
        """Return (token count, counting method, safety margin) for a prompt.

        When apply_template is supported, count the chat template rendered by
        the server; otherwise count the raw messages and keep a larger margin.
        """
        messages = self.messages(instruction, content)
        if self.config.get("apply_template"):
            template_request: dict[str, object] = {"messages": messages}
            policy = self.config.get("thinking_" + self.current_stage, "inherit")
            if (
                policy != "inherit"
                and self.config.get("thinking_adapter") == "chat_template"
            ):
                template_request["chat_template_kwargs"] = {
                    "enable_thinking": policy == "on"
                }
            payload = self.json_request(
                "POST", "/apply-template", json=template_request
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("prompt"), str
            ):
                raise ModelError(
                    "invalid_template",
                    "The backend did not return a rendered chat template.",
                )
            return self.tokenize(payload["prompt"]), "rendered_template", 64
        return (
            self.tokenize("\n".join(m["content"] for m in messages)),
            "estimated_template",
            384,
        )

    def fits(
        self, instruction: str, content: str, output_tokens: int
    ) -> tuple[bool, int, str, int]:
        """Tell whether a prompt and output_tokens fit the context.

        Return (fits, prompt tokens, counting method, margin). The limit is the
        job's context_tokens, or the server's context when it is smaller.
        """
        count, source, margin = self.prompt_size(instruction, content)
        limit = self.config["context_tokens"]
        engine = self.capabilities.get("context_tokens")
        if type(engine) is int and engine > 0:
            limit = min(limit, engine)
        return count + output_tokens + margin <= limit, count, source, margin

    def stream_lines(self, payload):
        """Send a streamed chat completion request and yield its raw lines.

        A reader thread does the network I/O, so this loop can enforce the step
        deadline and cancellation even while the server stays silent. Only one
        stream runs at a time: the next one waits, cancellably, until the
        previous reader has exited, and its deadline starts after that wait.
        Failed generations are not retried.
        """
        self.check_cancelled()
        while not _stream_gate.acquire(timeout=0.2):
            self.check_cancelled()
        events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=32)
        stopped = threading.Event()
        response_holder = []
        deadline = time.monotonic() + self.config["step_timeout_seconds"]

        def emit(kind, value):
            """Queue an event for the consuming loop; give up once that loop has stopped."""
            while not stopped.is_set():
                try:
                    events.put((kind, value), timeout=0.2)
                    return
                except queue.Full:
                    pass

        def read():
            """Reader thread: send the request and queue each line received."""
            try:
                with self.session.post(
                    self.base_url + "/v1/chat/completions",
                    json=payload,
                    stream=True,
                    timeout=(10, self.config["request_timeout"]),
                ) as response:
                    response_holder.append(response)
                    if stopped.is_set():
                        return
                    response.raise_for_status()
                    # Requests defaults text/* without a charset to Latin-1.
                    # Keep bytes until iter_lines has assembled a whole line:
                    # a UTF-8 code point can span several network chunks.
                    for line in response.iter_lines(
                        chunk_size=64, decode_unicode=False
                    ):
                        if stopped.is_set():
                            break
                        if line and len(line) > 1024 * 1024:
                            raise ModelError(
                                "invalid_stream",
                                "A streaming event exceeded the size limit.",
                            )
                        emit("line", line)
                emit("end", None)
            except Cancelled:
                emit("cancelled", None)
            except Exception:
                emit("error", None)
            finally:
                _stream_gate.release()

        reader = threading.Thread(target=read, name="llama-stream-reader", daemon=True)
        try:
            self.check_cancelled()
            reader.start()
        except BaseException:
            _stream_gate.release()
            raise
        try:
            while True:
                self.check_cancelled()
                if time.monotonic() >= deadline:
                    raise ModelError(
                        "deadline", "The generation exceeded its configured time limit."
                    )
                try:
                    kind, value = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "end":
                    return
                if kind == "cancelled":
                    raise Cancelled()
                if kind == "error":
                    raise ModelError(
                        "stream_error", "The model stream failed or disconnected."
                    )
                yield value
        finally:
            stopped.set()
            if reader.is_alive() and response_holder:
                close = getattr(response_holder[0], "close", None)
                if close:
                    # Some HTTP stacks block close() during a read: close from another thread.
                    threading.Thread(
                        target=close, name="llama-stream-close", daemon=True
                    ).start()
            reader.join(timeout=0.25)

    def generate(
        self,
        instruction: str,
        content: str,
        max_tokens: int,
        *,
        on_update: Callable[[str, str, bool], None] | None = None,
    ) -> CompletionResult:
        """Stream one completion and return a CompletionResult.

        on_update(content, reasoning, completed) receives previews at most
        every 0.8 seconds, and once at the end. Raise ConfigurationError when
        the prompt does not fit the context, and ModelError when the stream
        fails or the answer is truncated, empty or incomplete: such an answer
        is never saved.
        """
        started = time.monotonic()
        policy = self.config.get("thinking_" + self.current_stage, "inherit")
        parser = StreamParser(
            preview_limit=self.preview_limit,
            initial_thinking=self.config.get("initial_thinking", False),
            thinking_policy=policy,
            started=started,
        )
        metrics = parser.metrics
        metrics["output_budget"] = max_tokens
        metrics["request_started_at"] = time.time()
        self.current_metrics = metrics
        self.last_result = None
        latest = 0.0

        def publish(force=False):
            """Send the previews to on_update, at most every 0.8 seconds unless force is set."""
            nonlocal latest
            if on_update is None or (not force and time.monotonic() - latest < 0.8):
                return
            latest = time.monotonic()
            state = parser.state
            on_update(state.content, state.reasoning, state.completed)

        try:
            self.check_cancelled()
            fits, count, source, margin = self.fits(instruction, content, max_tokens)
            metrics.update(
                input_count=count, input_count_source=source, template_margin=margin
            )
            if not fits:
                raise ConfigurationError(
                    "context_insufficient",
                    "Prompt would exceed configured context. Start a new job with smaller budgets.",
                )
            payload = {
                "model": self.model(),
                "stream": True,
                "max_tokens": max_tokens,
                "messages": self.messages(instruction, content),
            }
            sampling: Mapping[str, object] = self.config
            for key in ("temperature", "top_p", "top_k", "min_p"):
                if self.config.get(key) is not None:
                    payload[key] = sampling[key]
            if self.config.get("usage_supported"):
                payload["stream_options"] = {"include_usage": True}
            if (
                policy != "inherit"
                and self.config.get("thinking_adapter") == "chat_template"
            ):
                payload["chat_template_kwargs"] = {"enable_thinking": policy == "on"}
            for line in self.stream_lines(payload):
                if parser.feed(line):
                    publish()
                if parser.done:
                    break
            answer = parser.finish()
            publish(force=True)
            self.last_result = CompletionResult(
                answer, cast(CompletionMetrics, metrics)
            )
            return self.last_result
        except WorkerStopping:
            metrics["stop_category"] = "interrupted"
            raise
        except Exception as exc:
            metrics["error_code"] = getattr(exc, "code", "context_or_generation_error")
            metrics["stop_category"] = (
                "cancelled" if isinstance(exc, Cancelled) else "error"
            )
            raise
        finally:
            metrics["request_ms"] = (time.monotonic() - started) * 1000
            parser.update_counts()

    def complete(
        self,
        instruction: str,
        content: str,
        max_tokens: int,
        *,
        on_update: Callable[[str, str, bool], None] | None = None,
    ) -> str:
        """Return only the answer text of generate()."""
        return self.generate(
            instruction, content, max_tokens, on_update=on_update
        ).content
