"""Discovery-mode AFD extractor.

Unlike production ``extract_afd_signals`` which forces a pre-baked 4-field
schema (the one shown to be uninformative in the 200-sample backtest), this
extractor asks the LLM for a richer, partially-open-ended set of features
designed for *feature discovery* rather than hypothesis testing.

The bet:
  * Many features are objectively extractable: explicit point forecast,
    explicit forecast range, mention of confidence words, mention of
    specific risk language, mention of model disagreement (with the
    direction of disagreement), mention of timing uncertainty.
  * Some are softer but still useful: the forecaster's "lean" direction
    (warmer/cooler than guidance), whether today's forecast is being
    revised vs. previous, whether the discussion flags specific bust risks.

We extract them all, then later test which ones (or combinations) predict
``observed_high - forecast_high_estimate`` or ``|observed_high - climatology|``
or ensemble-spread-implied uncertainty.

Output is JSON-formatted (because the model is gemma4:26b which handles
JSON without collapsing — unlike the 1B llama). Identity fallback to
production extractor if gemma is unreachable.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://68.80.186.101:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")
TIMEOUT_S = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))

# Slice the SYNOPSIS + NEAR/SHORT TERM. Same logic as production —
# import from the existing module so behavior matches.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from kalshi_weather.engines.afd_extractor import _slice_relevant_sections  # noqa: E402


# Designed to be answerable from the SYNOPSIS / NEAR / SHORT TERM portion
# of an AFD only — no LONG TERM speculation.
_DISCOVERY_PROMPT = """You are reading a National Weather Service Area Forecast Discussion.
Your task is to extract structured features about TODAY'S maximum temperature forecast.
Read carefully and answer in EXACT JSON format. Do not include any preamble or explanation.

JSON schema (all fields required, use null when a feature is not present in the text):

{{
  "high_point_f": <integer high temp in F, or null if only a range is given>,
  "high_range_low_f": <integer low end of range if a range is given, else null>,
  "high_range_high_f": <integer high end of range if a range is given, else null>,
  "confidence_word": <one of: "high", "moderate", "low", "uncertain", "good", "poor", null>,
  "explicit_uncertainty_phrases": [<list of short phrases (≤10 words each) the forecaster used to express uncertainty about today's high — e.g. "model spread remains high", "low confidence in timing">],
  "model_disagreement_mentioned": <true if forecaster explicitly says models disagree/diverge/spread/are inconsistent, else false>,
  "model_disagreement_direction": <"warmer", "cooler", "mixed", or null if no disagreement mentioned>,
  "forecaster_lean": <"warmer_than_guidance" / "cooler_than_guidance" / "blended" / "none_mentioned">,
  "bust_risk_words": [<list of short phrases indicating the forecaster sees specific bust risk — e.g. "cloud cover may limit heating", "frontal timing uncertain", "convection could disrupt mixing">],
  "synoptic_drivers": [<list of 1-4 short labels for the dominant synoptic features mentioned for today — free-form short noun phrases, e.g. "upper ridge", "cold front passage", "marine push", "anomalous warm advection", "post-frontal subsidence">],
  "timing_critical": <true if the forecast hinges on the timing of a feature (front, sea breeze, storms), else false>,
  "observational_anchor": <true if the forecaster references current obs or recent obs as anchoring the forecast, else false>,
  "revised_from_previous": <true if the discussion mentions revising/adjusting from prior package, else false>,
  "key_quote": <a single ≤30-word verbatim quote from the discussion that you think best captures the forecaster's mental model of today's high>
}}

Rules:
- "high_point_f" is the FORECAST high for today only (not tomorrow, not yesterday).
- If the forecast is "108 to 113 F", set high_point_f=null, high_range_low_f=108, high_range_high_f=113.
- If the forecast is "around 90 F", set high_point_f=90 and the range fields to null.
- Strings should be from the AFD text where possible, not paraphrased.
- Lists should have 0-5 items each.
- key_quote must be verbatim from the AFD.

DISCUSSION:
\"\"\"
{afd_text}
\"\"\"

JSON:"""


@dataclass(frozen=True, slots=True)
class DiscoveryFeatures:
    # Numeric forecast
    high_point_f: int | None
    high_range_low_f: int | None
    high_range_high_f: int | None
    # Confidence
    confidence_word: str | None
    explicit_uncertainty_phrases: tuple[str, ...]
    # Model disagreement
    model_disagreement_mentioned: bool | None
    model_disagreement_direction: str | None
    # Forecaster lean
    forecaster_lean: str | None
    # Risk vocabulary
    bust_risk_words: tuple[str, ...]
    # Synoptic
    synoptic_drivers: tuple[str, ...]
    timing_critical: bool | None
    observational_anchor: bool | None
    revised_from_previous: bool | None
    key_quote: str | None
    # Audit
    raw_llm_response: str
    extraction_failed: bool


_INT_KEYS = {"high_point_f", "high_range_low_f", "high_range_high_f"}
_BOOL_KEYS = {"model_disagreement_mentioned", "timing_critical",
              "observational_anchor", "revised_from_previous"}
_STR_KEYS = {"confidence_word", "model_disagreement_direction",
             "forecaster_lean", "key_quote"}
_LIST_KEYS = {"explicit_uncertainty_phrases", "bust_risk_words", "synoptic_drivers"}


def _coerce(obj: dict) -> dict:
    """Best-effort coerce LLM-returned values to the right types."""
    out: dict[str, Any] = {}
    for k in _INT_KEYS:
        v = obj.get(k)
        if isinstance(v, (int, float)) and -40 <= float(v) <= 140:
            out[k] = int(v)
        elif isinstance(v, str):
            m = re.search(r"-?\d+", v)
            out[k] = int(m.group()) if m and -40 <= int(m.group()) <= 140 else None
        else:
            out[k] = None
    for k in _BOOL_KEYS:
        v = obj.get(k)
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v.strip().lower() in ("true", "yes", "1")
        else:
            out[k] = None
    for k in _STR_KEYS:
        v = obj.get(k)
        out[k] = v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() != "null" else None
    for k in _LIST_KEYS:
        v = obj.get(k)
        if isinstance(v, list):
            cleaned = tuple(str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip())
        elif isinstance(v, str) and v.strip():
            cleaned = (v.strip(),)
        else:
            cleaned = ()
        out[k] = cleaned[:5]
    return out


def extract_discovery_features(afd_text: str) -> DiscoveryFeatures:
    if not afd_text or len(afd_text.strip()) < 50:
        return _failed("")

    sliced = _slice_relevant_sections(afd_text, 2800)
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": _DISCOVERY_PROMPT.format(afd_text=sliced)}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 600},
        "keep_alive": "2h",
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
        body = json.loads(raw_body)
        raw_response = (body.get("message") or {}).get("content", "") or ""
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return _failed("")

    # Strip code fences if model added them
    text = raw_response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        obj = json.loads(text)
    except Exception:
        # Try to find the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return _failed(raw_response[:500])
        try:
            obj = json.loads(m.group())
        except Exception:
            return _failed(raw_response[:500])

    if not isinstance(obj, dict):
        return _failed(raw_response[:500])

    c = _coerce(obj)
    return DiscoveryFeatures(
        high_point_f=c["high_point_f"],
        high_range_low_f=c["high_range_low_f"],
        high_range_high_f=c["high_range_high_f"],
        confidence_word=c["confidence_word"],
        explicit_uncertainty_phrases=c["explicit_uncertainty_phrases"],
        model_disagreement_mentioned=c["model_disagreement_mentioned"],
        model_disagreement_direction=c["model_disagreement_direction"],
        forecaster_lean=c["forecaster_lean"],
        bust_risk_words=c["bust_risk_words"],
        synoptic_drivers=c["synoptic_drivers"],
        timing_critical=c["timing_critical"],
        observational_anchor=c["observational_anchor"],
        revised_from_previous=c["revised_from_previous"],
        key_quote=c["key_quote"],
        raw_llm_response=raw_response[:1500],
        extraction_failed=False,
    )


def _failed(raw: str) -> DiscoveryFeatures:
    return DiscoveryFeatures(
        high_point_f=None,
        high_range_low_f=None,
        high_range_high_f=None,
        confidence_word=None,
        explicit_uncertainty_phrases=(),
        model_disagreement_mentioned=None,
        model_disagreement_direction=None,
        forecaster_lean=None,
        bust_risk_words=(),
        synoptic_drivers=(),
        timing_critical=None,
        observational_anchor=None,
        revised_from_previous=None,
        key_quote=None,
        raw_llm_response=raw,
        extraction_failed=True,
    )


if __name__ == "__main__":
    import time
    for fn in ["PHX_2026-05-10.json", "NYC_2026-04-20.json", "AUS_2026-04-09.json"]:
        with open(os.path.join(os.path.dirname(__file__), "afd_archive", fn)) as f:
            afd = json.load(f)["raw_text"]
        t0 = time.time()
        r = extract_discovery_features(afd)
        dt = time.time() - t0
        print(f"\n=== {fn} ({dt:.1f}s, failed={r.extraction_failed}) ===")
        for field_name in ("high_point_f", "high_range_low_f", "high_range_high_f",
                           "confidence_word", "model_disagreement_mentioned",
                           "model_disagreement_direction", "forecaster_lean",
                           "timing_critical", "observational_anchor",
                           "revised_from_previous"):
            print(f"  {field_name}: {getattr(r, field_name)}")
        print(f"  synoptic_drivers: {list(r.synoptic_drivers)}")
        print(f"  uncertainty_phrases: {list(r.explicit_uncertainty_phrases)}")
        print(f"  bust_risk_words: {list(r.bust_risk_words)}")
        print(f"  key_quote: {r.key_quote!r}")
