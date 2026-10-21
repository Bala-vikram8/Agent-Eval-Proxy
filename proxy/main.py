import os
import time
import json
import httpx
import psycopg2
from psycopg2.extras import Json
from fastapi import FastAPI, Request, Response
import uvicorn

app = FastAPI(title="AgentEval Proxy")

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TARGET_BASE = os.getenv("UPSTREAM_BASE", "https://api.openai.com")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def log_trace(request_id, model, messages, response, usage, latency_ms, tool_calls=None, status_code=200):
    try:
        conn = get_db()
        cur = conn.cursor()
        prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
        completion_tokens = usage.get("completion_tokens", 0) if usage else 0
        total_tokens = usage.get("total_tokens", 0) if usage else 0
        cost = (prompt_tokens * 0.00000015) + (completion_tokens * 0.0000006)
        cur.execute(
            """
            INSERT INTO traces (request_id, model, messages, response, prompt_tokens, completion_tokens, total_tokens, latency_ms, cost_usd, tool_calls, status_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (request_id, model, Json(messages), Json(response), prompt_tokens, completion_tokens, total_tokens, latency_ms, cost, Json(tool_calls or []), status_code)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Log error: {e}")

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(request: Request, path: str):
    start = time.time()
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    if OPENAI_API_KEY:
        headers["authorization"] = f"Bearer {OPENAI_API_KEY}"

    url = f"{TARGET_BASE}/v1/{path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
        )

    latency_ms = int((time.time() - start) * 1000)
    request_id = headers.get("x-request-id", f"req-{int(time.time()*1000)}")

    try:
        if not path.startswith("chat/completions"):
            raise ValueError("skip non-chat")
        data = json.loads(body) if body else {}
        model = data.get("model", "unknown")
        messages = data.get("messages", [])
        if resp.status_code == 200 and "text/event-stream" not in resp.headers.get("content-type", ""):
            resp_data = resp.json() if resp.content else {}
            usage = resp_data.get("usage")
            tool_calls = None
            if "choices" in resp_data:
                for choice in resp_data["choices"]:
                    if "message" in choice and "tool_calls" in choice["message"]:
                        tool_calls = choice["message"]["tool_calls"]
            log_trace(request_id, model, messages, resp_data, usage, latency_ms, tool_calls, resp.status_code)
        else:
            log_trace(request_id, model, messages, {"raw": resp.text[:500]}, None, latency_ms, None, resp.status_code)
    except ValueError:
        pass
    except Exception as e:
        print(f"Parse error: {e}")

    response_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")
    }

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)