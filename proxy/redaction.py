"""Pattern based redaction applied to traces before they are written anywhere.

This is data minimisation for obvious identifiers, not de-identification: names, addresses,
dates of birth and free text clinical details are NOT detected. Claim numbers, procedure and
reason codes, amounts and dates are deliberately left alone because evaluation needs them.
"""
import re

PATTERNS = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")),
]
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_text(text: str) -> tuple[str, int]:
    count = 0
    for label, pattern in PATTERNS:
        text, n = pattern.subn(f"[REDACTED_{label}]", text)
        count += n

    def card(match: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            count += 1
            return "[REDACTED_CARD]"
        return match.group(0)

    return CARD.sub(card, text), count


def redact(value) -> tuple[object, int]:
    """Redact every string inside a JSON-like value. Returns (new value, replacements)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        out, total = [], 0
        for item in value:
            new, n = redact(item)
            out.append(new)
            total += n
        return out, total
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            new, n = redact(item)
            out[key] = new
            total += n
        return out, total
    return value, 0
