"""Incremental parser for OpenAI style server sent event streams.

The proxy forwards each event to the client as soon as it is complete, and this class
rebuilds the final assistant message from the deltas so the stream can be logged like a
normal completion.
"""
import json


class StreamLimitError(ValueError):
    """The upstream exceeded the configured SSE framing limit."""


class StreamAccumulator:
    def __init__(self, hide_usage_chunk: bool = False, max_event_bytes: int = 262144,
                 max_capture_bytes: int = 262144):
        # The proxy asks upstream for a usage chunk so it can record token counts. When the
        # caller did not ask for that chunk itself, it is removed from what the caller sees.
        self.hide_usage_chunk = hide_usage_chunk
        self._buffer = b""
        self._content: list[str] = []
        self._tool_calls: dict[int, dict] = {}
        self.usage: dict | None = None
        self.finish_reason: str | None = None
        self.response_id: str | None = None
        self.model: str | None = None
        self.first_token_at: float | None = None
        self.done = False
        self.max_event_bytes = max_event_bytes
        self.max_capture_bytes = max_capture_bytes
        self._captured_bytes = 0
        self.capture_truncated = False
        self.malformed = False

    def feed(self, data: bytes, now: float) -> list[bytes]:
        """Add raw bytes from upstream and return the complete events to forward."""
        # Combine before normalizing: CR and LF can arrive in separate reads.
        self._buffer = (self._buffer + data).replace(b"\r\n", b"\n")
        forward = []
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            if len(event) > self.max_event_bytes:
                raise StreamLimitError('SSE event exceeds size limit')
            if self._handle(event, now):
                forward.append(event + b"\n\n")
        if len(self._buffer) > self.max_event_bytes:
            raise StreamLimitError('SSE event exceeds size limit')
        return forward

    def flush(self) -> bytes:
        """Return any trailing bytes that never formed a complete event."""
        rest, self._buffer = self._buffer, b""
        return rest

    def _handle(self, event: bytes, now: float) -> bool:
        data_lines = [line[5:].strip() for line in event.split(b"\n") if line.startswith(b"data:")]
        if not data_lines:
            return True  # comments and keep alive lines pass through untouched
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            self.done = True
            return True
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError):
            self.malformed = True
            return True
        if not isinstance(chunk, dict):
            self.malformed = True
            return True

        self.response_id = self.response_id or (chunk.get("id") if isinstance(chunk.get('id'), str) else None)
        self.model = self.model or (chunk.get("model") if isinstance(chunk.get('model'), str) else None)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            self.malformed = True
            return True
        if not choices and chunk.get("usage") and self.hide_usage_chunk:
            return False

        for choice in choices:
            if not isinstance(choice, dict):
                self.malformed = True
                continue
            if choice.get("index", 0) != 0:
                continue  # only the first choice is recorded
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                self.malformed = True
                continue
            if isinstance(delta.get("content"), str) and delta['content']:
                self._mark_first_token(now)
                if self._reserve(delta['content']):
                    self._content.append(delta["content"])
            calls = delta.get('tool_calls')
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    self.malformed = True
                    continue
                index = call.get('index', 0)
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 128:
                    self.malformed = True
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    self.malformed = True
                    continue
                name = function.get('name') if isinstance(function.get('name'), str) else ''
                arguments = function.get('arguments') if isinstance(function.get('arguments'), str) else ''
                if not self._reserve(name + arguments):
                    continue
                self._mark_first_token(now)
                slot = self._tool_calls.setdefault(
                    index,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if isinstance(call.get("id"), str):
                    slot["id"] = call["id"][:128]
                slot["function"]["name"] += name
                slot["function"]["arguments"] += arguments
            if isinstance(choice.get("finish_reason"), str):
                self.finish_reason = choice["finish_reason"]
        return True

    def _reserve(self, text: str) -> bool:
        if self.capture_truncated:
            return False
        self._captured_bytes += len(text.encode())
        if self._captured_bytes > self.max_capture_bytes:
            self.capture_truncated = True
            self._content.clear()
            self._tool_calls.clear()
            return False
        return True

    def _mark_first_token(self, now: float) -> None:
        if self.first_token_at is None:
            self.first_token_at = now

    @property
    def tool_calls(self) -> list[dict]:
        return [self._tool_calls[i] for i in sorted(self._tool_calls)]

    def response(self) -> dict:
        """The stream rebuilt as a regular chat.completion object."""
        message = {"role": "assistant", "content": "".join(self._content) or None}
        if self._tool_calls:
            message["tool_calls"] = self.tool_calls
        return {
            "id": self.response_id,
            "object": "chat.completion",
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}],
            "usage": self.usage,
            "stream_completed": self.done,
            "capture_truncated": self.capture_truncated,
        }
