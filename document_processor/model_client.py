"""llama.cpp adapter with explicit provenance and bounded streaming previews."""

import hashlib
import json
import math
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import requests

from .config import ConfigurationError
from .errors import Cancelled, ModelError


@dataclass
class CompletionResult:
    """Keep answer text separate from the metrics safe to persist or export."""

    content: str
    metrics: dict


def numeric(value):
    """Accept only finite, nonnegative measurements returned by the engine."""
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def collect_metrics(event, metrics):
    """Normalize optional usage/timings without treating SSE fragments as tokens."""
    usage = event.get("usage") or {}
    timings = event.get("timings") or {}
    if not isinstance(usage, dict) or not isinstance(timings, dict):
        return
    mapping = {"prompt_tokens": usage.get("prompt_tokens"),
               "output_tokens": usage.get("completion_tokens"),
               "evaluated_prompt_tokens": timings.get("prompt_n"),
               "prefill_ms": timings.get("prompt_ms"), "decode_ms": timings.get("predicted_ms")}
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
    for name, tokens, elapsed in (("prefill_tokens_per_second", "evaluated_prompt_tokens", "prefill_ms"),
                                  ("decode_tokens_per_second", "output_tokens", "decode_ms")):
        if metrics.get(tokens) is not None and metrics.get(elapsed):
            metrics[name] = metrics[tokens] * 1000 / metrics[elapsed]
    # Engine fields are not universally consistent: expose no invented useful count.
    if (metrics.get("reasoning_tokens") is not None and metrics.get("output_tokens") is not None
            and metrics["reasoning_tokens"] <= metrics["output_tokens"]):
        metrics["useful_tokens"] = metrics["output_tokens"] - metrics["reasoning_tokens"]
        metrics["useful_tokens_source"] = "engine_derived"


class ThinkingSplitter:
    """Split fragmented tags; optionally start inside a verified template's think block."""

    def __init__(self, *, initial_thinking=False, reasoning_limit=None):
        self.content_parts, self.reasoning_parts = [], []
        self.pending = ""
        self.inside_think = initial_thinking
        self.reasoning_limit = reasoning_limit
        self.reasoning_chars = 0

    @property
    def content(self):
        """Return useful output; it is retained until the completed artifact is saved."""
        return "".join(self.content_parts)

    @property
    def reasoning(self):
        """Return only the retained reasoning tail when a limit is configured."""
        return "".join(self.reasoning_parts)

    def append(self, text):
        """Keep total character counters independently from retained reasoning text."""
        if self.inside_think:
            self.reasoning_chars += len(text)
            self.reasoning_parts.append(text)
            if self.reasoning_limit is not None:
                self.reasoning_parts = [self.reasoning[-self.reasoning_limit:]] if self.reasoning_limit else []
        else:
            self.content_parts.append(text)

    def feed(self, fragment):
        """Consume a delta while keeping partial delimiters for the next event."""
        self.pending += fragment
        while self.pending:
            marker = "</think>" if self.inside_think else "<think>"
            pos = self.pending.find(marker)
            if pos >= 0:
                self.append(self.pending[:pos])
                self.pending = self.pending[pos + len(marker):]
                self.inside_think = not self.inside_think
                continue
            keep = 0
            for size in range(1, min(len(marker), len(self.pending) + 1)):
                if self.pending.endswith(marker[:size]):
                    keep = size
            self.append(self.pending[:len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep:]
            break

    def finish(self):
        """Flush the final fragment without inventing an absent answer channel."""
        self.append(self.pending)
        self.pending = ""


# A cancelled HTTP read may take time to close. Never start another generation
# while its reader is still alive, even if the external server ignores disconnects.
_stream_gate = threading.Lock()


class ModelClient:
    """Use an administrator-owned endpoint; jobs can never choose an arbitrary URL."""

    def __init__(self, base_url, key, config, prompt, preview_limit=12000):
        self.base_url, self.config, self.prompt = base_url.rstrip("/"), config, prompt
        self.preview_limit = preview_limit
        self.session = requests.Session()
        if key:
            self.session.headers.update({"Authorization": f"Bearer {key}"})
        self.model_name = None
        self.capabilities: dict[str, Any] = {"context_tokens": None, "status": "unknown"}
        self.token_cache = OrderedDict()
        self.tokenization_calls = 0
        self.tokenization_ms = 0.0
        self.tokenization_cache_hits = 0
        self.last_result = None
        self.cancel_check = lambda: False
        self.current_stage = "section"
        self.current_metrics = {}

    def check_cancelled(self):
        """Check a cheap callback shared with the job cancellation state."""
        if self.cancel_check():
            raise Cancelled()

    def json_request(self, method, path, *, timeout=90, **kwargs):
        """Make a bounded request and suppress server bodies and credential-bearing URLs."""
        self.check_cancelled()
        try:
            with self.session.request(method, self.base_url + path, timeout=(5, timeout), **kwargs) as response:
                response.raise_for_status()
                # JSON from llama.cpp is UTF-8, independently of HTTP charset
                # guesses (or a proxy's misleading Content-Type).
                return json.loads(response.content.decode("utf-8"))
        except (requests.RequestException, ValueError):
            raise ModelError("backend_unavailable", "The model backend is unavailable or returned invalid data.") from None

    def inspect(self):
        """Read optional properties without inference or raw slot/prompt disclosure."""
        result: dict[str, Any] = {"status": "unavailable", "context_tokens": None, "slots": None,
                  "model": None, "template_hash": None, "version": None,
                  "sampling": {},
                  "source": "backend", "observed_at": time.time()}
        try:
            result["model"] = self.model().replace("\\", "/").split("/")[-1][:120]
            result["status"] = "available"
        except ModelError:
            self.capabilities = result
            return result
        try:
            props = self.json_request("GET", "/props", timeout=5)
            settings = props.get("default_generation_settings") or {}
            context = settings.get("n_ctx") if isinstance(settings, dict) else None
            if type(context) is int and context > 0:
                result["context_tokens"] = context
            parameters = settings.get("params", {}) if isinstance(settings, dict) else {}
            if isinstance(parameters, dict):
                result["sampling"] = {key: parameters[key] for key in
                    ("temperature", "top_p", "top_k", "min_p", "repeat_penalty")
                    if numeric(parameters.get(key)) is not None}
            slots = props.get("total_slots")
            if type(slots) is int and slots > 0:
                result["slots"] = slots
            template = props.get("chat_template")
            if isinstance(template, str):
                result["template_hash"] = hashlib.sha256(template.encode("utf-8")).hexdigest()
            # Do not serialize arbitrary build metadata: it may contain local paths.
            version = props.get("version")
            if isinstance(version, str) and len(version) < 80 and all(c.isalnum() or c in ".-_ " for c in version):
                result["version"] = version
        except (ModelError, AttributeError, TypeError):
            result["properties"] = "unsupported_or_unavailable"
        self.capabilities = result
        return result

    def tokenize(self, text):
        """Cache identical counts within this client/job using bounded hash keys."""
        self.check_cancelled()
        key = hashlib.sha256(text.encode("utf-8")).digest()
        if key in self.token_cache:
            self.tokenization_cache_hits += 1
            self.token_cache.move_to_end(key)
            return self.token_cache[key]
        started = time.monotonic()
        try:
            payload = self.json_request("POST", "/tokenize", json={"content": text, "add_special": False})
            if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), list):
                raise ModelError("invalid_tokenizer", "Unexpected response from /tokenize.")
            count = len(payload["tokens"])
        finally:
            self.tokenization_calls += 1
            self.tokenization_ms += (time.monotonic() - started) * 1000
        self.token_cache[key] = count
        if len(self.token_cache) > 1024:
            self.token_cache.popitem(last=False)
        return count

    def model(self):
        """Select the first advertised model, preserving the historical API behavior."""
        if self.model_name:
            return self.model_name
        payload = self.json_request("GET", "/v1/models", timeout=5)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not items or not isinstance(items[0], dict) or not isinstance(items[0].get("id"), str):
            raise ModelError("missing_model", "No model returned by /v1/models.")
        self.model_name = items[0]["id"]
        return self.model_name

    def messages(self, instruction, content):
        """Build independent messages and apply only an administrator-verified adapter."""
        policy = self.config.get("thinking_" + self.current_stage, "inherit")
        if policy != "inherit" and self.config.get("thinking_adapter") == "prompt":
            instruction += "\n" + ("/think" if policy == "on" else "/no_think")
        return [{"role": "system", "content": self.prompt},
                {"role": "user", "content": instruction + "\n\n" + content}]

    def prompt_size(self, instruction, content):
        """Count a rendered template when explicitly supported, otherwise reserve headroom."""
        messages = self.messages(instruction, content)
        if self.config.get("apply_template"):
            template_request = {"messages": messages}
            policy = self.config.get("thinking_" + self.current_stage, "inherit")
            if policy != "inherit" and self.config.get("thinking_adapter") == "chat_template":
                template_request["chat_template_kwargs"] = {"enable_thinking": policy == "on"}
            payload = self.json_request("POST", "/apply-template", json=template_request)
            if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
                raise ModelError("invalid_template", "The backend did not return a rendered chat template.")
            return self.tokenize(payload["prompt"]), "rendered_template", 64
        return self.tokenize("\n".join(m["content"] for m in messages)), "estimated_template", 384

    def fits(self, instruction, content, output_tokens):
        """Validate every real prompt against application and known engine limits."""
        count, source, margin = self.prompt_size(instruction, content)
        limit = self.config["context_tokens"]
        engine = self.capabilities.get("context_tokens")
        if type(engine) is int and engine > 0:
            limit = min(limit, engine)
        return count + output_tokens + margin <= limit, count, source, margin

    def stream_lines(self, payload):
        """Poll a bounded reader queue so a silent prefill cannot defeat a deadline.

        Closing a socket is delegated to a daemon because some HTTP stacks wait
        for an ongoing read. The gate prevents overlapping local generations
        until the reader actually exits. Waiting is cancellable and does not
        consume the next generation's deadline. No generation retry is made.
        """
        self.check_cancelled()
        while not _stream_gate.acquire(timeout=0.2):
            self.check_cancelled()
        events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=32)
        stopped = threading.Event()
        response_holder = []
        deadline = time.monotonic() + self.config["step_timeout_seconds"]

        def emit(kind, value):
            """Apply backpressure without trapping the reader after cancellation."""
            while not stopped.is_set():
                try:
                    events.put((kind, value), timeout=0.2)
                    return
                except queue.Full:
                    pass

        def read():
            """Keep network I/O off the deadline/cancellation monitoring thread."""
            try:
                with self.session.post(self.base_url + "/v1/chat/completions", json=payload,
                                       stream=True, timeout=(10, self.config["request_timeout"])) as response:
                    response_holder.append(response)
                    if stopped.is_set():
                        return
                    response.raise_for_status()
                    # Requests defaults text/* without a charset to Latin-1.
                    # Keep bytes until iter_lines has assembled a whole line:
                    # a UTF-8 code point can span several network chunks.
                    for line in response.iter_lines(chunk_size=64, decode_unicode=False):
                        if stopped.is_set():
                            break
                        if line and len(line) > 1024 * 1024:
                            raise ModelError("invalid_stream", "A streaming event exceeded the size limit.")
                        emit("line", line)
                emit("end", None)
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
                    raise ModelError("deadline", "The generation exceeded its configured time limit.")
                try:
                    kind, value = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "end":
                    return
                if kind == "error":
                    raise ModelError("stream_error", "The model stream failed or disconnected.")
                yield value
        finally:
            stopped.set()
            if reader.is_alive() and response_holder:
                close = getattr(response_holder[0], "close", None)
                if close:
                    threading.Thread(target=close, name="llama-stream-close", daemon=True).start()
            reader.join(timeout=0.25)

    def generate(self, instruction, content, max_tokens, *, on_update=None):
        """Return a structured completion; incomplete output never becomes an artifact."""
        started = time.monotonic()
        metrics: dict[str, Any] = {key: None for key in ("prompt_tokens", "output_tokens", "reasoning_tokens",
                   "useful_tokens", "cached_prompt_tokens", "evaluated_prompt_tokens", "prefill_ms", "decode_ms",
                   "first_event_ms", "first_content_ms", "finish_reason")}
        metrics["output_budget"] = max_tokens
        metrics["request_started_at"] = time.time()
        self.current_metrics = metrics
        self.last_result = None
        policy = self.config.get("thinking_" + self.current_stage, "inherit")
        splitter = ThinkingSplitter(initial_thinking=self.config.get("initial_thinking", False) and policy != "off",
                                    reasoning_limit=self.preview_limit)
        reasoning, reasoning_chars = "", 0
        latest, completed, finish_reason = 0.0, False, None

        def publish(force=False):
            """Throttle disk/UI work independently from token arrival frequency."""
            nonlocal latest
            if on_update is None or (not force and time.monotonic() - latest < 0.8):
                return
            latest = time.monotonic()
            metrics["content_chars"] = sum(map(len, splitter.content_parts))
            metrics["reasoning_chars"] = reasoning_chars + splitter.reasoning_chars
            on_update(splitter.content, (splitter.reasoning + reasoning)[-self.preview_limit:], completed)

        try:
            self.check_cancelled()
            fits, count, source, margin = self.fits(instruction, content, max_tokens)
            metrics.update(input_count=count, input_count_source=source, template_margin=margin)
            if not fits:
                raise ConfigurationError("context_insufficient", "Prompt would exceed configured context. Start a new job with smaller budgets.")
            payload = {"model": self.model(), "stream": True, "max_tokens": max_tokens,
                       "messages": self.messages(instruction, content)}
            for key in ("temperature", "top_p", "top_k", "min_p"):
                if self.config.get(key) is not None:
                    payload[key] = self.config[key]
            if self.config.get("usage_supported"):
                payload["stream_options"] = {"include_usage": True}
            if policy != "inherit" and self.config.get("thinking_adapter") == "chat_template":
                payload["chat_template_kwargs"] = {"enable_thinking": policy == "on"}
            for line in self.stream_lines(payload):
                if not line:
                    continue
                if not isinstance(line, str):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError:
                        raise ModelError("invalid_stream_encoding", "The model stream is not valid UTF-8.") from None
                if not line.startswith("data:"):
                    continue
                message = line[5:].strip()
                if message == "[DONE]":
                    completed = True
                    break
                try:
                    event = json.loads(message)
                except ValueError:
                    raise ModelError("invalid_stream", "Malformed llama.cpp streaming response.") from None
                if not isinstance(event, dict) or "error" in event:
                    raise ModelError("stream_error", "The backend reported a streaming error.")
                if metrics["first_event_ms"] is None:
                    metrics["first_event_ms"] = (time.monotonic() - started) * 1000
                collect_metrics(event, metrics)
                choices = event.get("choices") or []
                if not isinstance(choices, list):
                    raise ModelError("invalid_stream", "Malformed streaming choices.")
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise ModelError("invalid_stream", "Malformed streaming choice.")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise ModelError("invalid_stream", "Malformed streaming delta.")
                    part = delta.get("reasoning_content") or delta.get("reasoning")
                    # A server-separated channel has already applied its template's
                    # reasoning parser; do not classify its useful content a second time.
                    if isinstance(part, str) and part and self.config.get("initial_thinking"):
                        splitter.inside_think = False
                    if isinstance(delta.get("content"), str):
                        splitter.feed(delta["content"])
                        if splitter.content_parts and metrics["first_content_ms"] is None:
                            metrics["first_content_ms"] = (time.monotonic() - started) * 1000
                    if isinstance(part, str):
                        reasoning_chars += len(part)
                        reasoning = (reasoning + part)[-self.preview_limit:]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                if sum(map(len, splitter.content_parts)) > 4_000_000:
                    raise ModelError("output_too_large", "The generated response exceeded the safety limit.")
                publish()
            splitter.finish()
            metrics["finish_reason"] = finish_reason if finish_reason in ("stop", "length", "content_filter", "tool_calls") else None
            if finish_reason == "length":
                raise ModelError("output_limit", "Model response hit max_tokens or the context limit; start a new job with adjusted budgets.")
            if finish_reason not in (None, "stop"):
                raise ModelError("unsupported_finish", "The backend returned an unsupported completion reason.")
            if not completed and finish_reason is None:
                raise ModelError("incomplete_stream", "llama.cpp stream closed before completion.")
            completed = True
            metrics["stop_category"] = "stop" if finish_reason == "stop" else "done_without_reason"
            answer = splitter.content.strip()
            if not answer:
                raise ModelError("empty_output", "Model returned empty content; start a new job with adjusted budgets.")
            publish(force=True)
            self.last_result = CompletionResult(answer, metrics)
            return self.last_result
        except Exception as exc:
            metrics["error_code"] = getattr(exc, "code", "context_or_generation_error")
            metrics["stop_category"] = "cancelled" if isinstance(exc, Cancelled) else "error"
            raise
        finally:
            metrics["request_ms"] = (time.monotonic() - started) * 1000
            metrics["content_chars"] = sum(map(len, splitter.content_parts))
            metrics["reasoning_chars"] = reasoning_chars + splitter.reasoning_chars

    def complete(self, instruction, content, max_tokens, *, on_update=None):
        """Preserve the historical string-returning interface for pipeline callers."""
        return self.generate(instruction, content, max_tokens, on_update=on_update).content
