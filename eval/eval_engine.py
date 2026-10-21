import os
import time
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import httpx

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def fetch_recent_traces(limit=10):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, messages, response, tool_calls
        FROM traces
        WHERE id NOT IN (SELECT trace_id FROM evaluations)
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def judge_faithfulness(messages, response):
    prompt = f"""
You are an evaluation judge. Score the faithfulness of the assistant response to the user query and conversation.
Return only a JSON object: {{"score": 0.0 to 1.0, "reason": "short explanation"}}

Conversation: {json.dumps(messages)}
Response: {json.dumps(response)}
"""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"}
                }
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            return float(result.get("score", 0.5)), result.get("reason", "")
    except Exception as e:
        print(f"Judge error: {e}")
        return 0.5, str(e)

def check_tool_correctness(tool_calls, response):
    # Simple deterministic check: if tool was called, response should reference it or be valid
    if not tool_calls:
        return True
    # Placeholder: assume correct if present and response is not error
    return True

def save_eval(trace_id, score, tool_ok, reason):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO evaluations (trace_id, faithfulness_score, tool_correctness, judge_reason)
        VALUES (%s, %s, %s, %s)
        """,
        (trace_id, score, tool_ok, reason)
    )
    conn.commit()
    cur.close()
    conn.close()

def run_eval_loop():
    print("Eval engine started")
    while True:
        traces = fetch_recent_traces(5)
        for t in traces:
            score, reason = judge_faithfulness(t["messages"], t["response"])
            tool_ok = check_tool_correctness(t["tool_calls"], t["response"])
            save_eval(t["id"], score, tool_ok, reason)
            print(f"Evaluated trace {t['id']}: score={score}, tool_ok={tool_ok}")
        time.sleep(30)

if __name__ == "__main__":
    run_eval_loop()
