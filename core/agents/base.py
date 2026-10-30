import os
import re
import threading
import time
from collections import deque
import instructor
from litellm import completion
from pydantic import BaseModel
from typing import Type, TypeVar, Optional

T = TypeVar('T', bound=BaseModel)

# Built once at import: instructor registers providers lazily and that is not thread-safe,
# so creating clients inside parallel graph nodes can fail with a RegistryError.
_client = instructor.from_litellm(completion)

_calls: deque = deque()
_tokens: dict = {}  # model -> deque of [monotonic time, tokens]; LLM_TPM is a per-model limit
_lock = threading.Lock()
_model_locks: dict = {}  # one in-flight call per model when LLM_TPM is set (parallel calls blow the per-minute budget)
_first_call = threading.Lock()
_registry_ready = threading.Event()
usage = {"calls": 0, "tokens": 0}  # running totals, handy for measuring a run

def _throttle() -> None:
    """Block until fewer than LLM_RPM calls were made in the last 60s (0 = unlimited). Shared by all threads."""
    rpm = int(os.environ.get("LLM_RPM", "0"))
    if rpm <= 0:
        return
    while True:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] > 60:
                _calls.popleft()
            if len(_calls) < rpm:
                _calls.append(now)
                return
            wait = 60 - (now - _calls[0]) + 0.1
        time.sleep(wait)

def _wait_for_tokens(model: str, estimate: int) -> list | None:
    """Block until `estimate` more tokens fit in the LLM_TPM per-minute budget (0 = unlimited).
    Reserves them right away (parallel nodes would otherwise all see an empty window); returns the reservation."""
    tpm = int(os.environ.get("LLM_TPM", "0"))
    if tpm <= 0:
        return None
    while True:
        with _lock:
            window = _tokens.setdefault(model, deque())
            now = time.monotonic()
            while window and now - window[0][0] > 60:
                window.popleft()
            used = sum(e[1] for e in window)
            if used + estimate <= tpm or not window:
                slot = [now, estimate]
                window.append(slot)
                return slot
            wait = 60 - (now - window[0][0]) + 0.1
        time.sleep(wait)

def _record_tokens(slot: list | None, n: int) -> None:
    with _lock:
        if slot is not None:
            slot[1] = n  # replace the estimate with the real count
        usage["calls"] += 1
        usage["tokens"] += n

def deep_model() -> str:
    """Model for the Research Manager and Portfolio Manager."""
    return os.environ.get("LLM_DEEP_MODEL") or os.environ.get("LLM_MODEL", "gemini/gemini-3.5-flash")

class BaseAgent:
    def __init__(self, role_prompt: str, model: str = None):
        self.role_prompt = role_prompt
        # Pull from env or default to a cheap gemini model
        self.model = model or os.environ.get("LLM_MODEL", "gemini/gemini-3.5-flash")
        
        self.client = _client

    def _create(self, messages: list, response_format: Type[T]):
        # instructor loads its provider registry on the first call and that is not thread-safe, so until
        # one call has gone through, parallel graph nodes take turns.
        first = not _registry_ready.is_set()
        if first:
            _first_call.acquire()
        gate = _model_locks.setdefault(self.model, threading.Lock()) if os.environ.get("LLM_TPM") else None
        if gate:
            gate.acquire()
        try:
            out = self.client.chat.completions.create_with_completion(
                model=self.model,
                messages=messages,
                response_model=response_format,
                reasoning_effort=os.environ.get("LLM_REASONING", "low"),
                timeout=120,
                num_retries=5,
            )
            _registry_ready.set()
            return out
        finally:
            if gate:
                gate.release()
            if first:
                _first_call.release()

    def analyze(self, data_context: str, response_format: Type[T]) -> T:
        """
        Sends the data context to the LLM and forces the output into the provided Pydantic schema.
        """
        messages = [
            {"role": "system", "content": self.role_prompt},
            {"role": "user", "content": data_context}
        ]
        
        # conservative estimate: prompt chars/2.5 plus room for reasoning + the structured reply
        slot = _wait_for_tokens(self.model, int(sum(len(m["content"]) for m in messages) / 2.5) + 1500)
        for attempt in range(8):
            _throttle()
            try:
                response, raw = self._create(messages, response_format)
                break
            except Exception as e:  # instructor wraps litellm errors, so match on the text
                if "tool_use_failed" in str(e) and attempt < 2:
                    continue  # the provider rejected a malformed tool call; just ask again
                if "RateLimitError" not in str(e):
                    raise
                # per-minute caps clear quickly: wait the time the provider suggests
                m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
                wait = int(m.group(1) or 0) * 60 + float(m.group(2)) + 1 if m else 0
                if attempt == 7 or not m or wait > 90:  # long waits mean a daily cap: fail fast
                    raise
                time.sleep(wait)

        _record_tokens(slot, getattr(raw.usage, "total_tokens", 0) or 0)
        return response
