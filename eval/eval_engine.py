import os
import time
import json
import psycopg2
from psycopg2.extras import Json
from openai import OpenAI

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

def get_db():
    return psycopg2.connect(DATABASE_URL)

def check_tool_correctness(tool_calls, expected_tools=None):
    """Return True only if at least one tool call is present and well-formed.
    Returns None if no tools were expected or present (not a failure)."""
    if not tool_calls:
        return None
    for tc in tool_calls:
        if not isinstance(tc, dict) or "function" not in tc:
            return False
        fn = tc["function"]
        if not fn.get("name"):
            return False
    return True

def judge_faithfulness(messages, response_text):
    if not response_text or not messages:
        return None
    prompt = f"""You are an evaluation judge. Score the assistant response for answer relevance (0.0 to 1.0).
User request and assistant response are below.
Return only a JSON object: {{"score": 0.0-1.0, "reason": "short explanation"}}

User: {json.dumps(messages)}
Assistant: {response_text}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        content = resp.choices[0].message.content
        parsed = json.loads(content)
        return float(parsed.get("score", None)), parsed.get("reason", "")
    except Exception as e:
        print(f"Judge error: {e}")
        return None, str(e)

def run_eval_loop():
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, messages, response, tool_calls, status_code
                FROM traces
                WHERE faithfulness_score IS NULL
                  AND status_code = 200
                ORDER BY created_at DESC
                LIMIT 5
            """)
            rows = cur.fetchall()
            for row in rows:
                tid, messages, response, tool_calls, status = row
                response_text = ""
                if isinstance(response, dict) and "choices" in response:
                    response_text = response["choices"][0].get("message", {}).get("content", "")
                score, reason = judge_faithfulness(messages, response_text)
                tool_ok = check_tool_correctness(tool_calls)
                cur.execute(
                    """
                    UPDATE traces
                    SET faithfulness_score = %s,
                        faithfulness_reason = %s,
                        tool_correct = %s
                    WHERE id = %s
                    """,
                    (score, reason, tool_ok, tid)
                )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Eval loop error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    run_eval_loop()