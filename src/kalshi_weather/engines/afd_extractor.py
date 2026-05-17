"""Extract structured weather signals from NWS AFD text via local LLM (Ollama).

The AFD is a multi-paragraph narrative. We use llama3.2:3b running locally
(via Ollama at http://localhost:11434) to extract:
  - confidence: forecaster's stated confidence in today's high
  - model_spread_flag: forecaster flagging significant model disagreement
  - regime: dominant synoptic feature (marine_layer, ridge, trough,
    frontal_passage, convective, stable, other)
  - mentioned_today_high_f: explicit forecaster point estimate, if any

Design notes:
  - All errors map to None — bot operates without AFD if Ollama is down.
  - LLM call has hard 20s timeout to protect cycle budget.
  - Returns deterministic JSON (temperature=0).
  - We do NOT trust quantitative extractions (the LLM is small); regime +
    confidence flags are the primary signal.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:latest"
OLLAMA_TIMEOUT_SECONDS = 20

_VALID_CONFIDENCE = {"low", "moderate", "high"}
_VALID_REGIMES = {
    "marine_layer",
    "ridge",
    "trough",
    "frontal_passage",
    "convective",
    "stable",
    "anomalous_warm",
    "anomalous_cool",
    "other",
}

_PROMPT = """You are extracting structured signals from a National Weather Service Area Forecast Discussion (AFD).

Return ONLY a JSON object with these exact keys (no prose):
{{
  "confidence": "low" | "moderate" | "high",
  "model_spread_flag": true | false,
  "regime": "marine_layer" | "ridge" | "trough" | "frontal_passage" | "convective" | "stable" | "anomalous_warm" | "anomalous_cool" | "other",
  "mentioned_today_high_f": <integer or null>
}}

Rules:
- "confidence" = forecaster's stated confidence in today's HIGH temperature specifically.
- "model_spread_flag" = true if forecaster explicitly says models disagree, are spread, or show low agreement.
- "regime" = single dominant synoptic feature mentioned for today.
- "mentioned_today_high_f" = explicit point estimate in °F if any, else null. Do NOT guess.

AFD text:
\"\"\"
{afd_text}
\"\"\"

JSON:"""


@dataclass(frozen=True, slots=True)
class AfdExtraction:
    confidence: str | None
    model_spread_flag: bool | None
    regime: str | None
    mentioned_today_high_f: int | None
    raw_llm_response: str
    extraction_failed: bool


def extract_afd_signals_cached(
    *, wfo: str, product_id: str, afd_text: str,
    cache_dir: str = "data/cache/afd_extractions",
) -> AfdExtraction:
    """Same as extract_afd_signals but caches by (wfo, product_id).

    AFDs only update every ~6h while we cycle every 30 min — caching by the
    product's stable id means we extract each AFD once and reuse 12+ times.
    """
    import os
    safe_pid = "".join(c for c in product_id if c.isalnum() or c in "-._")[:80]
    cache_path = os.path.join(cache_dir, f"{wfo}_{safe_pid}.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                data = json.load(f)
            return AfdExtraction(
                confidence=data.get("confidence"),
                model_spread_flag=data.get("model_spread_flag"),
                regime=data.get("regime"),
                mentioned_today_high_f=data.get("mentioned_today_high_f"),
                raw_llm_response=data.get("raw_llm_response", ""),
                extraction_failed=bool(data.get("extraction_failed", False)),
            )
        except Exception:
            pass  # cache corrupt — fall through to fresh extract

    result = extract_afd_signals(afd_text)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "confidence": result.confidence,
                "model_spread_flag": result.model_spread_flag,
                "regime": result.regime,
                "mentioned_today_high_f": result.mentioned_today_high_f,
                "raw_llm_response": result.raw_llm_response,
                "extraction_failed": result.extraction_failed,
            }, f)
    except Exception:
        pass  # cache write failure is non-fatal
    return result


def extract_afd_signals(afd_text: str) -> AfdExtraction:
    """Call Ollama to extract structured signals. Returns AfdExtraction with
    extraction_failed=True (and Nones) on any failure — never raises.
    """
    if not afd_text or len(afd_text.strip()) < 50:
        return AfdExtraction(
            confidence=None,
            model_spread_flag=None,
            regime=None,
            mentioned_today_high_f=None,
            raw_llm_response="",
            extraction_failed=True,
        )

    # Truncate to a sane size — AFDs can be 5-10KB; the parts we care about
    # are usually in the first 3000 chars (NEAR TERM / DISCUSSION sections).
    truncated = afd_text[:3500]

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": _PROMPT.format(afd_text=truncated),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 200},
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return AfdExtraction(
            confidence=None,
            model_spread_flag=None,
            regime=None,
            mentioned_today_high_f=None,
            raw_llm_response="",
            extraction_failed=True,
        )

    try:
        body = json.loads(raw_body)
        raw_response = body.get("response", "")
        parsed = json.loads(raw_response)
    except Exception:
        return AfdExtraction(
            confidence=None,
            model_spread_flag=None,
            regime=None,
            mentioned_today_high_f=None,
            raw_llm_response=raw_body[:500],
            extraction_failed=True,
        )

    # Sanitize each field — never trust LLM output blindly.
    confidence = parsed.get("confidence")
    if confidence not in _VALID_CONFIDENCE:
        confidence = None

    spread_flag = parsed.get("model_spread_flag")
    if not isinstance(spread_flag, bool):
        spread_flag = None

    regime = parsed.get("regime")
    if regime not in _VALID_REGIMES:
        regime = "other" if regime else None

    high_f = parsed.get("mentioned_today_high_f")
    if isinstance(high_f, (int, float)) and -40 <= float(high_f) <= 140:
        high_f = int(high_f)
    else:
        high_f = None

    return AfdExtraction(
        confidence=confidence,
        model_spread_flag=spread_flag,
        regime=regime,
        mentioned_today_high_f=high_f,
        raw_llm_response=raw_response[:500],
        extraction_failed=False,
    )
