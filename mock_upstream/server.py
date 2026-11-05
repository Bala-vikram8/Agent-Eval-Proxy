"""Offline stand in for the OpenAI chat completions API.

Used by the offline demo, the tests and the overhead benchmark, so the whole stack runs
without an API key. Replies are deterministic. It also answers judge requests from the
eval worker with stub verdicts based on keyword overlap. Those stubs only exercise the
pipeline; they are not a real evaluation and their "accuracy" means nothing.

Test triggers, placed in the last user message:
  [mock-drop-stream]  a stream sends two chunks, then the connection is cut
  [mock-sleep:N]      wait N seconds before answering
MOCK_SCRIPT_PATH points at a JSON list of scripted replies (see scripted_reply) so a demo
can supply its own canned model output without changing this file.
"""
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LATENCY_MS = int(os.getenv("MOCK_LATENCY_MS", "0"))
JITTER_MS = int(os.getenv("MOCK_JITTER_MS", "0"))
STREAM_DELAY_MS = int(os.getenv("MOCK_STREAM_DELAY_MS", "0"))
MODELS = ("gpt-4o-mini", "mock-small")
# Like OpenAI, an alias is answered by a dated snapshot, which the response reports.
SNAPSHOTS = {"gpt-4o-mini": "gpt-4o-mini-2024-07-18"}
JUDGE_MARKER = "You grade whether an AI assistant's reply addresses the user's request."
GROUNDED_MARKER = "You check whether an AI assistant's reply is supported by the evidence it was given."
SCRIPT = json.loads(Path(os.environ["MOCK_SCRIPT_PATH"]).read_text()) if os.getenv("MOCK_SCRIPT_PATH") else []
TOOL_TRIGGERS = ("invoice", "billing", "charge", "refund", "account")
STOPWORDS = {
    "about", "after", "again", "their", "there", "these", "this", "that", "with", "from", "have",
    "your", "will", "would", "what", "when", "which", "while", "user", "users", "next", "action",
    "priority", "ticket", "please", "thanks", "customer", "support",
}

app = FastAPI(title="Mock OpenAI upstream")
seen_authorization: list[str | None] = []


@app.get("/mock/seen-authorization")
async def seen():
    """Lets tests check which key the proxy forwarded."""
    return {"authorization": seen_authorization[-50:]}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    seen_authorization.append(request.headers.get("authorization"))
    body = await request.json()
    model = body.get("model")
    messages = body.get("messages") or []
    if model not in MODELS:
        return JSONResponse(status_code=404, content={"error": {
            "message": f"The model `{model}` does not exist or you do not have access to it.",
            "type": "invalid_request_error", "param": None, "code": "model_not_found"}})

    await asyncio.sleep(delay_seconds(messages))
    last_user = next((text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")
    sleep = re.search(r"\[mock-sleep:(\d+(?:\.\d+)?)\]", last_user)
    if sleep:
        await asyncio.sleep(float(sleep.group(1)))

    if messages and JUDGE_MARKER in text_of(messages[0]):
        return judge(model, messages)
    if messages and GROUNDED_MARKER in text_of(messages[0]):
        return grounded_judge(model, messages)

    message = scripted_reply(body, messages) or reply_for(body, messages)
    usage = usage_for(messages, message)
    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        drop = "[mock-drop-stream]" in last_user
        return StreamingResponse(stream(model, message, usage, include_usage, drop), media_type="text/event-stream")
    return completion(model, message, usage)


@app.post("/v1/responses")
async def responses(request: Request):
    """Minimal streamed Responses API, only used to check the proxy relays it incrementally."""
    body = await request.json()

    async def events():
        for word in ("one", "two", "three", "four"):
            yield sse({"type": "response.output_text.delta", "delta": word})
            await asyncio.sleep(STREAM_DELAY_MS / 1000)
        yield sse({"type": "response.completed"})

    if body.get("stream"):
        return StreamingResponse(events(), media_type="text/event-stream")
    return {"object": "response", "output_text": "one two three four"}


def scripted_reply(body: dict, messages: list) -> dict | None:
    """First matching rule from MOCK_SCRIPT_PATH. A rule matches when every condition holds:
    system_contains (substring of the system prompt), user_contains (substring of the last user
    message), last_role (role of the last message), has_tools (tools offered)."""
    if not SCRIPT or not messages:
        return None
    system = " ".join(text_of(m) for m in messages if m.get("role") in ("system", "developer"))
    user = next((text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")
    tools_seen = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    for rule in SCRIPT:
        when = rule.get("when", {})
        if "system_contains" in when and when["system_contains"] not in system:
            continue
        if "user_contains" in when and when["user_contains"] not in user:
            continue
        if "last_role" in when and messages[-1].get("role") != when["last_role"]:
            continue
        if "tool_results" in when and len(tools_seen) != when["tool_results"]:
            continue
        reply = rule["reply"]
        if "tool_calls" in reply:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{hashlib.sha256((user + c['name']).encode()).hexdigest()[:12]}", "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                for c in reply["tool_calls"]]}
        return {"role": "assistant", "content": reply["content"]}
    return None


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def delay_seconds(messages: list) -> float:
    jitter = 0
    if JITTER_MS:
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).digest()
        jitter = int.from_bytes(digest[:4], "big") % (JITTER_MS + 1)
    return (LATENCY_MS + jitter) / 1000


def reply_for(body: dict, messages: list) -> dict:
    last = messages[-1] if messages else {}
    user = next((text_of(m) for m in reversed(messages) if m.get("role") == "user"), "").lower()
    system = " ".join(text_of(m) for m in messages if m.get("role") == "system").lower()

    if last.get("role") == "tool":
        return {"role": "assistant", "content": (
            f"Checked the account: {text_of(last)} The charge is a billing error, so refund the "
            "$12.50 difference and send the customer a corrected invoice.")}

    tools = body.get("tools") or []
    if tools and any(word in user for word in TOOL_TRIGGERS):
        name = tools[0].get("function", {}).get("name", "lookup")
        call_id = "call_" + hashlib.sha256(user.encode()).hexdigest()[:12]
        arguments = json.dumps({"query": user[:60]})
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}

    if "reply to the customer" in system:
        ticket = user.split("\n")[0]  # the demo puts the original ticket on the first line
        if "login" in ticket or "password" in ticket:
            text = "Hi, we have escalated your login problem to IT and will confirm the password reset fix before payroll runs."
        elif "invoice" in ticket or "billing" in ticket:
            text = "Hi, our billing team is correcting the $12.50 discrepancy on your invoice and will email you the new copy."
        elif "profile picture" in ticket:
            text = "Hi, open Settings, choose Profile, then select Change picture to upload a new profile picture."
        else:
            text = "Hi, thanks for reaching out. Your ticket is with our support team and we will update you shortly."
    elif "login" in user or "password" in user:
        text = "Priority: urgent. The user is locked out before payroll, so escalate to IT and check why the password reset email is not arriving."
    elif "profile picture" in user and "triage" in system:
        # Deliberately off topic, so the dashboard has a low relevance example to show.
        text = "Priority: low. The latest invoice shows a $12.50 adjustment that will appear on the next statement."
    elif "profile picture" in user:
        text = "To change the profile picture, open Settings, choose Profile, then select Change picture."
    elif "invoice" in user or "billing" in user:
        text = "Priority: normal. Confirm the $12.50 billing discrepancy and route the invoice to the billing team."
    else:
        text = "Priority: normal. Acknowledge the ticket and route it to the general support queue."
    return {"role": "assistant", "content": text}


def usage_for(messages: list, message: dict) -> dict:
    prompt = sum(len(text_of(m)) for m in messages) // 4 + 4 * len(messages)
    output = message.get("content") or json.dumps(message.get("tool_calls"))
    completion_tokens = len(output) // 4 + 1
    return {"prompt_tokens": prompt, "completion_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens}


def completion(model: str, message: dict, usage: dict) -> dict:
    return {
        "id": "chatcmpl-mock-" + hashlib.sha256(json.dumps(message).encode()).hexdigest()[:16],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": SNAPSHOTS.get(model, model),
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        "usage": usage,
    }


def sse(payload) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return f"data: {data}\n\n".encode()


async def stream(model: str, message: dict, usage: dict, include_usage: bool, drop: bool = False):
    base = {"id": "chatcmpl-mock-stream", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": SNAPSHOTS.get(model, model)}
    extra = {"usage": None} if include_usage else {}

    def chunk(delta: dict, finish_reason=None) -> bytes:
        return sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra})

    yield chunk({"role": "assistant", "content": ""})
    if drop:
        yield chunk({"content": "Partial answer "})
        await asyncio.sleep(0.05)
        raise ConnectionAbortedError("mock upstream cut the stream")
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        arguments = call["function"]["arguments"]
        half = len(arguments) // 2
        yield chunk({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                     "function": {"name": call["function"]["name"], "arguments": ""}}]})
        for piece in (arguments[:half], arguments[half:]):
            await asyncio.sleep(STREAM_DELAY_MS / 1000)
            yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
        finish = "tool_calls"
    else:
        words = message["content"].split(" ")
        for i in range(0, len(words), 3):
            await asyncio.sleep(STREAM_DELAY_MS / 1000)
            piece = " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")
            yield chunk({"content": piece})
        finish = "stop"
    yield chunk({}, finish)
    if include_usage:
        yield sse({**base, "choices": [], "usage": usage})
    yield sse("[DONE]")


def keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}


def judge(model: str, messages: list):
    exchange = json.loads(text_of(messages[-1]))
    request_text = exchange.get("user_request", "")
    reply_text = exchange.get("assistant_reply", "")
    if "[judge-fail]" in reply_text:
        return JSONResponse(status_code=500, content={"error": {"message": "mock judge failure", "type": "server_error"}})
    shared = keywords(request_text) & keywords(reply_text)
    score = 1.0 if len(shared) >= 2 else 0.5 if shared else 0.0
    verdict = {"score": score, "reason": f"Stub judge: {len(shared)} shared keywords"
               + (f" ({', '.join(sorted(shared)[:4])})." if shared else ".")}
    message = {"role": "assistant", "content": json.dumps(verdict)}
    return completion(model, message, usage_for(messages, message))


NOT_FOUND = ("not found", "no record", "no policy", "unavailable")


def grounded_judge(model: str, messages: list):
    """Heuristic stand in for the grounded LLM judge. A sentence is "supported" when most of
    its content words occur in the evidence, otherwise "unsupported". It never reports
    "contradicted" and never finds missing information: it cannot read meaning."""
    case = json.loads(text_of(messages[-1]))
    reply = case.get("reply", "")
    if "[judge-fail]" in reply:
        return JSONResponse(status_code=500, content={"error": {"message": "mock judge failure", "type": "server_error"}})
    evidence = case.get("evidence") or []
    evidence_words = keywords(" ".join(e.get("text", "") for e in evidence))
    claims = []
    for sentence in re.split(r"(?<=[.!?])\s+", reply.strip()):
        words = keywords(sentence)
        if not words:
            continue
        overlap = len(words & evidence_words) / len(words)
        cited = [e["id"] for e in evidence if words & keywords(e.get("text", ""))][:3]
        claims.append({"text": sentence, "label": "supported" if overlap >= 0.6 else "unsupported",
                       "evidence_ids": cited, "explanation": f"stub: {overlap:.0%} of content words in evidence"})
    verdict = {
        "addresses_request": bool(keywords(case.get("request", "")) & keywords(reply)),
        "claims": claims,
        "evidence_sufficient": not any(marker in e.get("text", "").lower() for e in evidence for marker in NOT_FOUND),
        "missing_information": [],
        "reason": "Stub grounded judge (keyword overlap), not a real evaluation.",
    }
    message = {"role": "assistant", "content": json.dumps(verdict)}
    return completion(model, message, usage_for(messages, message))
