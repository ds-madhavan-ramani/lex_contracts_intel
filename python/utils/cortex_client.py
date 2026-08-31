"""
cortex_client.py — thin wrapper around SNOWFLAKE.CORTEX.COMPLETE / AI_COMPLETE.

Unchanged from the project-llm-wiki template.
Model is passed in per call (from ProjectConfig.active_model) rather than
read from a module-level ACTIVE_MODEL constant, so two projects running in
the same process can use different models.
"""

import json
import time
from typing import Optional

from utils.logging_utils import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2


class CortexError(Exception):
    pass


def complete(session, model: str, prompt: str, max_tokens: int = 4096,
             temperature: float = 0.0) -> str:
    """Call Cortex AI_COMPLETE with retry + basic validation. Returns text."""
    if not prompt or not prompt.strip():
        raise CortexError("Empty prompt passed to complete()")

    options = json.dumps({"max_tokens": max_tokens, "temperature": temperature})

    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        start = time.time()
        try:
            result = session.sql(
                "SELECT AI_COMPLETE(?, ?, PARSE_JSON(?)) AS RESPONSE",
                params=[model, prompt, options],
            ).collect()
            latency_ms = int((time.time() - start) * 1000)
            text = result[0]["RESPONSE"]
            logger.info("EVENT=CORTEX_CALL model=%s latency_ms=%d attempt=%d",
                        model, latency_ms, attempt)
            return text
        except Exception as e:  # noqa: BLE001 — deliberately broad, retried below
            last_err = e
            logger.warning("EVENT=CORTEX_RETRY model=%s attempt=%d error=%s",
                           model, attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY_SECONDS ** attempt)

    raise CortexError(f"Cortex call failed after {MAX_RETRIES} attempts: {last_err}")


_json_decoder = json.JSONDecoder()


def _strip_leading_markdown_fence(text: str) -> str:
    """Strips a leading ```json (or bare ```) code-fence marker, if
    present. Only the *opening* fence needs handling here — raw_decode()
    below parses just the first complete JSON value and ignores anything
    after it, so a stray closing fence (or trailing prose past it) doesn't
    need to be located/stripped separately."""
    text = text.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.lstrip()
    return text


_JSON_RETRY_SUFFIX = """

IMPORTANT: Your previous response was not valid JSON syntax. A common
cause is an unescaped double-quote character inside a string value (e.g.
quoting what someone said verbatim) — every " that appears INSIDE a
string value must be escaped as \\". Return ONLY the corrected JSON,
no commentary, with every internal double-quote properly escaped."""

# Cap for the token budget a truncation retry can request — well within
# what Cortex models on this account support, so the retry call itself
# doesn't fail for asking too much.
MAX_JSON_RETRY_TOKENS = 16000


def _is_truncation_error(error: json.JSONDecodeError, text: str) -> bool:
    """True when the response looks like it was cut off mid-value (hit
    max_tokens) rather than a genuine syntax mistake (e.g. an unescaped
    quote) — the two need different fixes: a bigger token budget vs. a
    corrective instruction asking for the same length of output again."""
    return "Unterminated string" in error.msg or error.pos >= len(text) - 2


def complete_json(session, model: str, prompt: str, max_tokens: int = 4096,
                   _is_retry: bool = False) -> dict:
    """Call complete() and parse the response as JSON, with a clear error on failure."""
    raw = complete(session, model, prompt, max_tokens=max_tokens)

    # Models don't reliably follow "return ONLY valid JSON, no commentary".
    # Two distinct failure shapes seen in production:
    #   (a) the whole fenced answer escaped as one JSON string —
    #       '"```json\n{...}\n```"' — rather than the object directly;
    #   (b) a valid JSON object followed by a prose explanation after the
    #       closing fence, e.g. '```json\n{...}\n```\n\nBased on my
    #       review...' — plain json.loads() rejects this as "Extra data"
    #       even though the JSON itself is perfectly valid.
    # raw_decode() parses just the first complete JSON value and ignores
    # anything trailing it, handling (b) directly; the loop below
    # additionally unwraps (a) by re-parsing when the result is itself a
    # JSON-encoded string.
    candidate = raw
    last_error: Optional[json.JSONDecodeError] = None
    parsed = None
    for _ in range(3):  # bounded: a couple of unwrap levels is plenty
        cleaned = _strip_leading_markdown_fence(candidate)
        try:
            parsed, _end_index = _json_decoder.raw_decode(cleaned)
        except json.JSONDecodeError as e:
            last_error = e
            parsed = None
            break
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            candidate = parsed  # one level of JSON-string-encoding unwrapped; retry
            continue
        break  # some other non-dict, non-str JSON value (e.g. a list) — give up

    # A genuine JSON syntax error (not just fencing/double-encoding, which
    # the loop above already handles) — most often an unescaped quote
    # inside a quoted statement in the source text — isn't something
    # re-parsing the same text can fix. Retry the LLM call itself once
    # with an explicit corrective instruction, since resampling plus a
    # nudge about escaping is the standard mitigation, before giving up.
    # Bounded to one retry (_is_retry guards against recursing further).
    if last_error is not None and not _is_retry:
        if _is_truncation_error(last_error, candidate) and max_tokens < MAX_JSON_RETRY_TOKENS:
            retry_max_tokens = min(max_tokens * 2, MAX_JSON_RETRY_TOKENS)
            logger.warning(
                "EVENT=JSON_PARSE_RETRY_TRUNCATED max_tokens=%d->%d response_tail=%r",
                max_tokens, retry_max_tokens, raw[-200:],
            )
            # Same prompt, bigger budget — a corrective instruction about
            # quote-escaping wouldn't fix a response that was simply cut
            # off before it finished.
            return complete_json(session, model, prompt,
                                  max_tokens=retry_max_tokens, _is_retry=True)
        logger.warning("EVENT=JSON_PARSE_RETRY response_preview=%r", raw[:500])
        return complete_json(session, model, prompt + _JSON_RETRY_SUFFIX,
                              max_tokens=max_tokens, _is_retry=True)

    # Either the final parse failed outright (even after a retry), or it
    # kept resolving to something that's never a dict (e.g. a genuine
    # refusal message). Include a raw preview in the raised message itself
    # (not just the server-side log) so it's visible directly in the
    # Streamlit error card, not just Snowsight's server-side logs.
    logger.error("EVENT=JSON_PARSE_ERROR response_preview=%r", raw[:500])
    if last_error is not None:
        raise CortexError(
            f"Cortex response was not valid JSON: {last_error} "
            f"— raw response preview: {raw[:300]!r}"
        ) from last_error
    raise CortexError(
        f"Cortex response was valid JSON but not a JSON object (got "
        f"{type(parsed).__name__}) — raw response preview: {raw[:300]!r}"
    )
