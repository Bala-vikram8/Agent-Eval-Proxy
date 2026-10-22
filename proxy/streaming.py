"""Incremental parser for OpenAI style server sent event streams.

The proxy forwards each event to the client as soon as it is complete, and this class
rebuilds the final assistant message from the deltas so the stream can be logged like a
normal completion.
"""
import json


class StreamAccumulator:
    def __init__(self, hide_usage_chunk: bool = False):
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

    def feed(self, data: bytes, now: float) -> list[bytes]:
        """Add raw bytes from upstream and return the complete events to forward."""
        self._buffer += data.replace(b"\r\n", b"\n")
        forward = []
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            if self._handle(event, now):
                forward.append(event + b"\n\n")
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
        except ValueError:
            return True
        if not isinstance(chunk, dict):
            return True

        self.response_id = self.response_id or chunk.get("id")
        self.model = self.model or chunk.get("model")
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices and chunk.get("usage") and self.hide_usage_chunk:
            return False

        for choice in choices:
            if choice.get("index", 0) != 0:
                continue  # only the first choice is recorded
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self._mark_first_token(now)
                self._content.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                self._mark_first_token(now)
                slot = self._tool_calls.setdefault(
                    call.get("index", 0),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                slot["function"]["name"] += function.get("name") or ""
                slot["function"]["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
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
        }
