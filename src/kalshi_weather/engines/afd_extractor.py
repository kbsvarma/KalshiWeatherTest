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
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
# Default model tag: ``llama3.2:3b`` matches what scripts/lightsail/bootstrap.sh
# pulls. Locally the Mac may have ``llama3.2:latest`` (an alias to 3b). Env-
# overrideable so cloud and dev can use different model sizes without code
# changes. 2026-05-19: was hard-coded to ``llama3.2:latest`` which doesn't
# resolve on Lightsail (only :3b was pulled there) — silently no-op'd every
# AFD extraction.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
# Timeout bumped 20→90 on 2026-05-19 PM after the 2-vCPU Lightsail box took
# >20s on first inference (cold-cache, full AFD). Extraction is cached by
# product_id so we only pay this cost ~4x per WFO per day. 90s is generous
# enough for slow cold-start while still bounded — fail-fast if the model
# isn't reachable, otherwise wait for the real answer.
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "90"))

# AFD_BACKEND selects which LLM API the extractor talks to:
#   "ollama"     (default) — POST http://OLLAMA_BASE_URL/api/generate
#                with Ollama's JSON body schema. Used by the Lightsail
#                box which runs llama3.2:1b locally.
#   "gemma_chat" — POST http://AFD_CHAT_URL/api/chat with body
#                {"content": "<prompt>"}. Custom NDJSON-streaming chat
#                interface fronted by nginx, hosted on a Gemma 26B box
#                accessible over the public IP (port 80). The endpoint
#                emits {"type":"thinking"} deltas during chain-of-thought
#                then {"type":"delta"} deltas for the final answer, then
#                one {"type":"done","message":{"content":...}} record
#                that carries the full answer.
# Falls back to ``ollama`` on any malformed/missing config so an empty
# env var can't break extraction.
AFD_BACKEND = os.environ.get("AFD_BACKEND", "ollama").strip().lower() or "ollama"
AFD_CHAT_URL = os.environ.get("AFD_CHAT_URL", "").rstrip("/")
AFD_CHAT_TIMEOUT_SECONDS = int(os.environ.get("AFD_CHAT_TIMEOUT_SECONDS", "180"))
# Max AFD text we send to the LLM. AFDs are ~6-10KB total but the
# forecast-confidence and today-high signals live almost entirely in the
# SYNOPSIS + NEAR/SHORT TERM sections, which are within the first ~2500
# chars. Sending less = faster inference + lower hallucination risk.
OLLAMA_AFD_MAX_CHARS = int(os.environ.get("OLLAMA_AFD_MAX_CHARS", "2500"))

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

_PROMPT = """Below is a National Weather Service Area Forecast Discussion. Read it and answer in EXACTLY this format, one value per line:

HIGH: <today's forecast high temperature in F, just the integer, or NA if not stated>
CONFIDENCE: <high or moderate or low>
SPREAD: <yes if forecaster says models disagree/spread/uncertain, else no>
PATTERN: <ridge or trough or frontal or convective or stable or marine_layer or anomalous_warm or anomalous_cool or other>

DISCUSSION:
{afd_text}

ANSWER:
"""


def _slice_relevant_sections(afd_text: str, max_chars: int) -> str:
    """Extract the parts of an AFD most relevant to today's forecast.

    AFDs follow a stable section structure (NWS Directive 10-503):
      .SYNOPSIS...      — large-scale pattern
      .NEAR TERM...     — next ~6h (some offices)
      .SHORT TERM...    — today through tomorrow
      .LONG TERM...     — day 3+
      .AVIATION...      — irrelevant for daily-high
      .MARINE...        — irrelevant
      .${WFO} WATCHES/WARNINGS... — alerts

    We want SYNOPSIS + NEAR/SHORT TERM. They contain the forecaster's
    confidence in TODAY's high, model-disagreement callouts, and the
    regime descriptor. Falling back to head-of-AFD if section markers
    aren't found preserves the previous behavior.
    """
    if not afd_text:
        return ""
    upper = afd_text.upper()
    relevant_markers = (".SYNOPSIS", ".NEAR TERM", ".SHORT TERM", ".DISCUSSION", ".KEY MESSAGES")
    stop_markers = (".LONG TERM", ".AVIATION", ".MARINE", ".HYDROLOGY", ".FIRE WEATHER", ".CLIMATE")
    starts = [upper.find(m) for m in relevant_markers]
    starts = [s for s in starts if s >= 0]
    if not starts:
        return afd_text[:max_chars]
    start = min(starts)
    # Find the first stop marker AFTER the start
    stops = [upper.find(m, start) for m in stop_markers]
    stops = [s for s in stops if s > start]
    end = min(stops) if stops else start + max_chars
    sliced = afd_text[start:end][:max_chars]
    return sliced if sliced.strip() else afd_text[:max_chars]


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

    # Prefer the SYNOPSIS + NEAR TERM / SHORT TERM sections — that's where
    # today's forecast confidence lives. Fall back to head-of-AFD if we
    # can't find them. Limiting to OLLAMA_AFD_MAX_CHARS keeps inference
    # fast on small CPUs.
    truncated = _slice_relevant_sections(afd_text, OLLAMA_AFD_MAX_CHARS)

    prompt_text = _PROMPT.format(afd_text=truncated)
    raw_response = ""

    if AFD_BACKEND == "ollama_chat":
        # Standard Ollama /api/chat endpoint with reasoning disabled.
        # Used when the configured Ollama model supports `think: false`
        # (Gemma 3/4, Qwen2.5 reasoning variants). Plain non-streaming
        # POST; response.message.content carries the answer.
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 80},
            "keep_alive": "2h",
        }
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw_body)
            raw_response = (body.get("message") or {}).get("content", "") or ""
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return AfdExtraction(
                confidence=None,
                model_spread_flag=None,
                regime=None,
                mentioned_today_high_f=None,
                raw_llm_response="",
                extraction_failed=True,
            )

    elif AFD_BACKEND == "gemma_chat" and AFD_CHAT_URL:
        # Custom NDJSON-streaming chat endpoint. Body: {"content": <prompt>}.
        # Read until {"type":"done", ...}, take message.content.
        req = urllib.request.Request(
            f"{AFD_CHAT_URL}/api/chat",
            data=json.dumps({"content": prompt_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=AFD_CHAT_TIMEOUT_SECONDS) as resp:
                for line in resp:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "done":
                        msg = rec.get("message") or {}
                        raw_response = msg.get("content", "") or ""
                        break
        except (urllib.error.URLError, OSError, TimeoutError):
            return AfdExtraction(
                confidence=None,
                model_spread_flag=None,
                regime=None,
                mentioned_today_high_f=None,
                raw_llm_response="",
                extraction_failed=True,
            )
        if not raw_response:
            return AfdExtraction(
                confidence=None,
                model_spread_flag=None,
                regime=None,
                mentioned_today_high_f=None,
                raw_llm_response="",
                extraction_failed=True,
            )
    else:
        # Ollama (default). Single non-streaming POST → {"response": "<text>"}.
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt_text,
            "stream": False,
            # 2026-05-19: dropped "format": "json". Forcing JSON-mode on a
            # 1B model collapsed output to a constant default JSON regardless
            # of input. Plain-text key:value + regex parse works reliably.
            "options": {"temperature": 0, "num_predict": 80},
            "keep_alive": "2h",
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
            raw_response = body.get("response", "") or ""
        except Exception:
            return AfdExtraction(
                confidence=None,
                model_spread_flag=None,
                regime=None,
                mentioned_today_high_f=None,
                raw_llm_response=raw_body[:500],
                extraction_failed=True,
            )

    # Parse plain-text "KEY: value" lines (case-insensitive). Tolerant
    # to leading numbering ("1. HIGH:"), markdown ("**HIGH:**"), and stray
    # punctuation.
    import re as _re
    def _line(key: str) -> str | None:
        m = _re.search(rf"^[\s\d\.\*\-]*{key}\s*[:\-]\s*(.+?)\s*$",
                       raw_response, _re.IGNORECASE | _re.MULTILINE)
        return m.group(1).strip() if m else None

    high_raw = _line("HIGH")
    conf_raw = (_line("CONFIDENCE") or "").lower()
    spread_raw = (_line("SPREAD") or "").lower()
    pattern_raw = (_line("PATTERN") or _line("REGIME") or "").lower()

    # Sanitize each field — never trust LLM output blindly.
    confidence = conf_raw if conf_raw in _VALID_CONFIDENCE else None

    if spread_raw in ("yes", "true", "y", "1"):
        spread_flag = True
    elif spread_raw in ("no", "false", "n", "0"):
        spread_flag = False
    else:
        spread_flag = None

    # Map model's short pattern label to our regime enum.
    _PATTERN_ALIASES = {
        "frontal": "frontal_passage",
        "front": "frontal_passage",
        "convection": "convective",
        "thunderstorm": "convective",
        "marine": "marine_layer",
        "anomalous": "other",  # too vague — model didn't pick warm/cool
        "warm": "anomalous_warm",
        "cool": "anomalous_cool",
        "cold": "anomalous_cool",
        "high pressure": "ridge",
        "low pressure": "trough",
    }
    if pattern_raw in _VALID_REGIMES:
        regime = pattern_raw
    elif pattern_raw in _PATTERN_ALIASES:
        regime = _PATTERN_ALIASES[pattern_raw]
    elif pattern_raw:
        regime = "other"
    else:
        regime = None

    # Build a parsed dict for the legacy validation paths below
    parsed = {"mentioned_today_high_f": None}
    if high_raw and high_raw.upper() not in ("NA", "N/A", "NONE", "NULL", ""):
        try:
            # Strip any non-digit characters (e.g. "103°F" → 103)
            n = int(_re.sub(r"[^\d\-]", "", high_raw))
            parsed["mentioned_today_high_f"] = n
        except Exception:
            parsed["mentioned_today_high_f"] = None

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
