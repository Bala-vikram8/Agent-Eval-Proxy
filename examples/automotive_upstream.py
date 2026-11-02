"""Local simulated automotive workflow model; never a real repair/customer service.

This deterministic fixture deliberately supports a failure switch. Bind only to
loopback and never deploy it as a provider or attach real credentials/data.
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="SIMULATED automotive workflow fixture")
app.state.broken = False
MODEL = "simulated-automotive-v1"
EXPECTED = "Route the work order to the parts-review queue."
BROKEN = "Close the work order and mark all parts delivered."


@app.get("/health")
async def health():
    return {"status": "ok", "simulated": True}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/scenario/config")
async def scenario_config(request: Request):
    body = await request.json()
    if type(body.get("broken")) is not bool:
        return JSONResponse(status_code=400, content={"error": "broken must be boolean"})
    app.state.broken = body["broken"]
    return {"simulated": True, "broken": app.state.broken}


@app.post("/v1/chat/completions")
async def completion(request: Request):
    payload = await request.json()
    if payload.get("model") != MODEL:
        return JSONResponse(status_code=404, content={"error": {"code": "model_not_found"}})
    messages = payload.get("messages", [])
    has_document = any(m.get("role") == "tool" for m in messages)
    if not has_document:
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "sim-document-call", "type": "function", "function": {
                "name": "lookup_workflow_document", "arguments": '{"document_id":"SIM-WO-7"}'}}]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": BROKEN if app.state.broken else EXPECTED}
        finish = "stop"
    usage = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    # Counts are synthetic fixture values, not tokenizer measurements or paid usage.
    base = {"id": "simulated-completion", "model": MODEL, "created": 0}
    if payload.get("stream") and has_document:
        async def chunks():
            for part in [message["content"][:20], message["content"][20:]]:
                yield ("data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {"content": part}, "finish_reason": None}]}) + "\n\n").encode()
            yield ("data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n").encode()
            if (payload.get("stream_options") or {}).get("include_usage"):
                yield ("data: " + json.dumps({**base, "choices": [], "usage": usage}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {**base, "object": "chat.completion", "choices": [
        {"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
