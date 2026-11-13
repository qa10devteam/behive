"""
Unified LLM abstraction for BeHive engine.

BeHive is BYOK (Bring Your Own Key). All pipeline stages call through this module.
It routes to whatever LLM the user configured — never assumes a specific provider.

Resolution order:
1. BEHIVE_MODEL env var (explicit: "anthropic/claude-sonnet-4-20250514", "openai/gpt-4o", etc.)
2. Per-stage override: BEHIVE_MODEL_SCOUT, BEHIVE_MODEL_PROCESS, BEHIVE_MODEL_SYNTH
3. Auto-detect from available API keys (first found wins)
4. Clear error message with setup instructions

Usage:
    from behive.engine.llm import complete, complete_async

    result = complete("Analyze this text", stage="process")
    result = await complete_async("Generate report", stage="synth", max_tokens=4096)
"""
import os
import json
import logging
import math
import time
from typing import Optional

# Ensure Bedrock knows our region (litellm needs this for cross-region inference profiles)
if not os.environ.get("AWS_REGION_NAME") and not os.environ.get("AWS_DEFAULT_REGION"):
    os.environ["AWS_REGION_NAME"] = "eu-central-1"

log = logging.getLogger(__name__)


# ─── Model Resolution ────────────────────────────────────────────────────────

# Suggested models per provider (used ONLY for auto-detection, never hardcoded)
_PROVIDER_SUGGESTIONS = {
    "anthropic": "anthropic/claude-sonnet-4-20250514",
    "openai": "openai/gpt-4o",
    "groq": "groq/llama-3.3-70b-versatile",
    "together": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "mistral": "mistral/mistral-large-latest",
    "deepseek": "deepseek/deepseek-chat",
    "bedrock": "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "local": "openai/local-model",
    "gemini": "gemini/gemini-3.6-flash",
}

# API key env vars → provider name
_KEY_TO_PROVIDER = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GROQ_API_KEY": "groq",
    "TOGETHER_API_KEY": "together",
    "MISTRAL_API_KEY": "mistral",
    "DEEPSEEK_API_KEY": "deepseek",
    "GOOGLE_API_KEY": "gemini",
    "GEMINI_API_KEY": "gemini",
}


def _get_configured_model(stage: str) -> str:
    """Get model for a pipeline stage. Provider-agnostic resolution."""
    
    # 1. Per-stage override (e.g. BEHIVE_MODEL_SYNTH=openai/gpt-4o)
    stage_model = os.environ.get(f"BEHIVE_MODEL_{stage.upper()}")
    if stage_model:
        return stage_model
    
    # 2. Global model setting
    global_model = os.environ.get("BEHIVE_MODEL")
    if global_model:
        return global_model
    
    # 3. Try behive config file
    try:
        from behive.config import get_model_for_stage
        configured = get_model_for_stage(stage)
        if configured:
            return configured
    except (ImportError, Exception):
        pass
    
    # 4. Auto-detect from available API keys (first found wins)
    for env_var, provider in _KEY_TO_PROVIDER.items():
        if os.environ.get(env_var):
            model = _PROVIDER_SUGGESTIONS[provider]
            log.debug(f"Auto-detected provider '{provider}' from {env_var}")
            return model
    
    # 5. Check for local LLM endpoint
    local_url = os.environ.get("BEHIVE_LLM_URL") or os.environ.get("BEHIVE_LOCAL_LLM_URL")
    if local_url:
        return _PROVIDER_SUGGESTIONS["local"]
    
    # 6. Check for AWS Bedrock (last resort — most complex setup)
    if _has_aws_credentials():
        return _PROVIDER_SUGGESTIONS["bedrock"]
    
    return ""


def _has_aws_credentials() -> bool:
    """Check if AWS credentials are available (env, file, or instance role)."""
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return True
    if os.path.exists(os.path.expanduser("~/.aws/credentials")):
        return True
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials()
        return creds is not None
    except Exception:
        return False


def _check_keys() -> str:
    """Return error message if no LLM credentials are configured."""
    model = _get_configured_model("process")
    if model:
        return ""
    
    return (
        "No LLM configured. BeHive uses YOUR OWN LLM key (BYOK).\n\n"
        "Set one of:\n"
        "  export ANTHROPIC_API_KEY=your-key-here    (Anthropic Claude)\n"
        "  export OPENAI_API_KEY=your-key-here       (OpenAI GPT-4o)\n"
        "  export GROQ_API_KEY=your-key-here         (Groq, free tier)\n"
        "  export MISTRAL_API_KEY=...             (Mistral)\n"
        "  export DEEPSEEK_API_KEY=...            (DeepSeek)\n"
        "  export BEHIVE_LLM_URL=http://localhost:11434/v1  (Ollama/vLLM/SGLang)\n"
        "  export AWS_PROFILE=default             (Bedrock)\n\n"
        "Or set model explicitly:\n"
        "  export BEHIVE_MODEL=anthropic/claude-sonnet-4-20250514\n\n"
        "All 100+ litellm providers supported. See: https://docs.litellm.ai/docs/providers\n"
    )


# ─── Completion functions ───────────────────────────────────────────────────


def _completion_timeout(default: Optional[float]) -> Optional[float]:
    """Optional timeout in seconds, validated before any completion is attempted."""
    value = os.environ.get("BEHIVE_LLM_TIMEOUT")
    if value is None:
        return default
    try:
        timeout = float(value)
    except ValueError:
        raise ValueError("BEHIVE_LLM_TIMEOUT must be finite seconds in (0, 3600]") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("BEHIVE_LLM_TIMEOUT must be finite seconds in (0, 3600]")
    return timeout


def _strict_completion_options(litellm, kwargs: dict) -> None:
    """Disable request retries/fallbacks without rewriting shared LiteLLM globals."""
    # Some LiteLLM versions use `request_value or global_value`, so passing 0/[]
    # alone cannot override unsafe globals. Reject them before sending anything.
    if (getattr(litellm, "num_retries", None)
            or getattr(litellm, "model_fallbacks", None) is not None):
        raise RuntimeError("BeHive strict mode requires LiteLLM retries/fallbacks disabled")
    kwargs.update(num_retries=0, max_retries=0, fallbacks=[], context_window_fallback_dict={})
    if kwargs["model"].startswith("openai/") and "OPENAI_API_BASE" in os.environ:
        api_base = os.environ["OPENAI_API_BASE"]
        if not api_base.strip():
            raise ValueError("OPENAI_API_BASE must not be empty in BeHive strict mode")
        kwargs["api_base"] = api_base


def _codex_bridge_enabled(strict: bool, model: str) -> bool:
    """Explicit route consent: omit unsupported sampling/token caps, never emulate them."""
    if os.environ.get("BEHIVE_LLM_CODEX_BRIDGE") != "1":
        return False
    if not strict:
        raise ValueError("Codex bridge requires BEHIVE_LLM_STRICT=1")
    if model != "openai/gpt-6.1-sol":
        raise ValueError("Codex bridge requires model openai/gpt-6.1-sol")
    if os.environ.get("OPENAI_API_BASE") != "http://127.0.0.1:8646/v1":
        raise ValueError("Codex bridge requires OPENAI_API_BASE=http://127.0.0.1:8646/v1")
    return True


_BRIDGE_BUSY_MESSAGE = "Local concurrency limit reached; retry later."

# ── Phase deadline (BEHIVE_PHASE_DEADLINE, epoch seconds, set by run_phase) ──────
# Every model call of a phase must end before the deadline, with time left to
# persist its result, and no call starts once too little time is left.
DEADLINE_MARGIN_S = 5.0         # kept after a call for committing its result
_MIN_CALL_S_DEFAULT = 10.0      # BEHIVE_LLM_MIN_CALL_S: never start a shorter call


class PhaseDeadlineExceeded(RuntimeError):
    """The phase deadline leaves no time for another model call; nothing was sent."""


def _now() -> float:
    return time.time()


def phase_deadline() -> Optional[float]:
    """BEHIVE_PHASE_DEADLINE as epoch seconds, or None when unset/invalid."""
    try:
        value = float(os.environ.get("BEHIVE_PHASE_DEADLINE", ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def deadline_remaining() -> Optional[float]:
    """Seconds a model call may still use (deadline minus margin); None: no deadline."""
    deadline = phase_deadline()
    if deadline is None:
        return None
    return deadline - _now() - DEADLINE_MARGIN_S


def _min_call_s() -> float:
    try:
        value = float(os.environ.get("BEHIVE_LLM_MIN_CALL_S", ""))
    except (TypeError, ValueError):
        return _MIN_CALL_S_DEFAULT
    return value if math.isfinite(value) and value >= 0 else _MIN_CALL_S_DEFAULT


def deadline_call_timeout(timeout: Optional[float], stage: str = "process") -> Optional[float]:
    """The call timeout capped by the phase deadline; raises before a call that
    could not finish in time (PhaseDeadlineExceeded)."""
    remaining = deadline_remaining()
    if remaining is None:
        return timeout
    if remaining < _min_call_s():
        log.info("LLM call not started: phase deadline (stage=%s remaining_s=%.1f)",
                 stage, max(remaining, 0.0))
        raise PhaseDeadlineExceeded(
            f"phase deadline leaves {max(remaining, 0.0):.0f}s for a model call")
    return remaining if timeout is None else min(timeout, remaining)


def is_deadline_timeout(exc: BaseException) -> bool:
    """A call cut by the phase deadline (refused, or its capped timeout fired)."""
    if isinstance(exc, PhaseDeadlineExceeded):
        return True
    remaining = deadline_remaining()
    if remaining is None or remaining > 0:
        return False
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name


def _local_bridge_busy(exc: Exception) -> bool:
    """Only the bridge's stable local 429 signal, never a provider quota denial."""
    if getattr(exc, "status_code", None) != 429 and type(exc).__name__ != "RateLimitError":
        return False
    text = str(exc)
    if ("upstream_rate_limit" in text
            or "Upstream quota/rate limit denied the request." in text):
        return False
    code = getattr(exc, "code", None)
    # LiteLLM replaces the original code/body with the generic string "429".
    codes = {code} if isinstance(code, str) and code != "429" else set()
    bodies = [getattr(exc, "body", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            bodies.append(response.json())
        except (AttributeError, TypeError, ValueError):
            pass
    for body in bodies:
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                continue
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(code, str):
                    codes.add(code)
    codes.discard(None)
    # A structured denial wins over coincidental busy text in a wrapped body.
    if codes:
        return codes == {"bridge_busy"}
    return _BRIDGE_BUSY_MESSAGE in str(exc)


class _BridgeBusyWait:
    """Per-call wait budget. Retry only the same, locally rejected request."""

    def __init__(self):
        try:
            limit = float(os.environ.get("BEHIVE_LLM_BUSY_WAIT_S", "90"))
        except ValueError:
            limit = 90.0
        self.limit = max(0.0, limit) if math.isfinite(limit) else 90.0
        self.started = time.monotonic()
        self.waited = 0.0
        self.retries = 0
        self.deadline_reached = False
        deadline = phase_deadline()
        self.deadline = deadline if deadline is not None else math.inf

    def delay(self, exc: Exception) -> float | None:
        if not _local_bridge_busy(exc):
            return None
        # Never sleep into the window where no call may start any more; stopping
        # there is the phase deadline's doing (callers defer), not a provider error.
        window = self.deadline - _now() - DEADLINE_MARGIN_S - _min_call_s()
        if window <= 0:
            self.deadline_reached = True
            return None
        remaining = min(self.limit - self.waited,
                        self.limit - (time.monotonic() - self.started),
                        window)
        if remaining <= 0:
            return None
        delay = min(5.0 * 2 ** min(self.retries, 3), 30.0, remaining)
        self.waited += delay
        self.retries += 1
        log.info("LLM local busy wait: retry=%s wait_s=%s error_type=%s",
                 self.retries, delay, type(exc).__name__)
        return delay

    def prepare_retry(self, kwargs: dict, timeout: Optional[float] = None, stage: str = "process") -> bool:
        """Re-cap the retry by the phase deadline (raises PhaseDeadlineExceeded when
        no call can start any more)."""
        kwargs["timeout"] = deadline_call_timeout(timeout if timeout is not None else kwargs.get("timeout"), stage)
        return True


def complete(
    prompt: str,
    stage: str = "process",
    system: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """
    Synchronous LLM completion. Routes to user's configured model.
    
    Args:
        prompt: User message / main prompt
        stage: Pipeline stage (scout, harvest, process, synth) — for per-stage model routing
        system: System prompt (optional)
        max_tokens: Max output tokens
        temperature: Sampling temperature
        json_mode: If True, request JSON output format
    
    Returns:
        Generated text (str). Legacy failures may fall back to direct APIs.
        BEHIVE_LLM_STRICT=1 propagates failures with no provider retries/fallback.
        Only local bridge_busy waits: 5/10/20/30 s backoff, capped by
        BEHIVE_LLM_BUSY_WAIT_S (default 90) and by the phase deadline: every call's
        timeout ends 5 s before BEHIVE_PHASE_DEADLINE, no call starts with less than
        BEHIVE_LLM_MIN_CALL_S (default 10 s) left (PhaseDeadlineExceeded), and the
        busy wait never sleeps into that window.
        BEHIVE_LLM_TIMEOUT: finite seconds in (0, 3600]; default 25 (strict: 180).
        BEHIVE_LLM_CODEX_BRIDGE=1 requires strict + exact model/loopback base;
        only that route omits temperature/max_tokens, without a hard token cap.
    """
    strict = os.environ.get("BEHIVE_LLM_STRICT") == "1"
    timeout = _completion_timeout(180 if strict else 25)
    error = _check_keys()
    if error:
        raise RuntimeError(error)
    
    model = _get_configured_model(stage)
    if not model:
        raise RuntimeError(
            f"No model configured for stage '{stage}'. "
            "Set BEHIVE_MODEL or an API key env var."
        )
    
    codex_bridge = _codex_bridge_enabled(strict, model)
    if codex_bridge:
        log.debug(f"LLM call: stage={stage} model={model}")
    else:
        log.debug(f"LLM call: stage={stage} model={model} tokens={max_tokens}")
    
    # litellm handles ALL providers (100+) — it's the primary path
    try:
        import litellm
        litellm.set_verbose = False
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        kwargs = {
            "model": model,
            "messages": messages,
            # Capped by the phase deadline; PhaseDeadlineExceeded before a call
            # that could not finish in time.
            "timeout": deadline_call_timeout(timeout, stage),
        }
        if not codex_bridge:
            kwargs.update(max_tokens=max_tokens, temperature=temperature)
        
        # Local LLM: set api_base
        local_url = os.environ.get("BEHIVE_LLM_URL") or os.environ.get("BEHIVE_LOCAL_LLM_URL")
        if local_url and ("local" in model or "ollama" in model):
            kwargs["api_base"] = local_url
            kwargs["api_key"] = "not-needed"
            # For local models, strip provider prefix
            model_name = model.split("/")[-1] if "/" in model else model
            kwargs["model"] = f"openai/{model_name}"
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        if strict:
            _strict_completion_options(litellm, kwargs)
            busy = _BridgeBusyWait()
            while True:
                try:
                    response = litellm.completion(**kwargs)
                    return response.choices[0].message.content or ""
                except Exception as exc:
                    delay = busy.delay(exc)
                    if delay is None:
                        if busy.deadline_reached:
                            raise PhaseDeadlineExceeded("bridge busy until the phase deadline") from None
                        raise
                    time.sleep(delay)
                    busy.prepare_retry(kwargs, timeout, stage)

        # Retry with exponential backoff (handles rate limits, transient failures)
        last_error = None
        for attempt in range(3):
            if attempt:
                # A retry is a new call: it must fit the phase deadline too.
                kwargs["timeout"] = deadline_call_timeout(timeout, stage)
            try:
                response = litellm.completion(**kwargs)
                return response.choices[0].message.content or ""
            except Exception as retry_err:
                last_error = retry_err
                err_str = str(retry_err).lower()
                # Don't retry on auth errors or invalid model
                if any(k in err_str for k in ('auth', 'invalid_api_key', 'permission', 'not found')):
                    raise
                if any(k in err_str for k in ('429', 'quota', 'resource exhausted', 'rate limit')):
                    raise
                # Retry on timeout, server error
                if attempt < 2:
                    wait = (attempt + 1) * 2  # 2s, 4s
                    log.debug("LLM retry %s/3 after %ss (%s)", attempt + 1, wait, type(retry_err).__name__)
                    import time as _time
                    _time.sleep(wait)
        
        # All retries exhausted
        if last_error:
            raise last_error
        
    except ImportError:
        if strict:
            raise
        log.warning("litellm not installed. Falling back to direct API calls.")
        log.warning("Install litellm for 100+ provider support: pip install litellm")
    except PhaseDeadlineExceeded:
        raise  # never fall through to another provider call after the deadline
    except Exception as e:
        if is_deadline_timeout(e):
            raise PhaseDeadlineExceeded("model call cut by the phase deadline") from None
        if strict:
            raise
        err_str = str(e).lower()
        # Auth errors should propagate (user needs to fix their key)
        if any(k in err_str for k in ('auth', 'invalid_api_key', 'permission', 'invalid api key', 'api_key')):
            raise
        log.error("LLM call failed (model=%s, error_type=%s)", model, type(e).__name__)
        # Fall through to direct calls as last resort
    
    # Fallback: direct API calls (supports Anthropic, OpenAI, Bedrock only)
    deadline_call_timeout(timeout, stage)  # no fallback call after the deadline
    return _direct_call(model, prompt, system, max_tokens, temperature)


async def complete_async(
    prompt: str,
    stage: str = "process",
    system: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """Async completion; strict errors propagate. Legacy async timeout stays unset."""
    strict = os.environ.get("BEHIVE_LLM_STRICT") == "1"
    timeout = _completion_timeout(180 if strict else None)
    error = _check_keys()
    if error:
        raise RuntimeError(error)
    
    model = _get_configured_model(stage)
    if not model:
        raise RuntimeError(f"No model configured for stage '{stage}'.")
    
    codex_bridge = _codex_bridge_enabled(strict, model)
    log.debug(f"LLM async call: stage={stage} model={model}")
    
    try:
        import litellm
        if strict:
            litellm.set_verbose = False
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        kwargs = {
            "model": model,
            "messages": messages,
        }
        if not codex_bridge:
            kwargs.update(max_tokens=max_tokens, temperature=temperature)
        timeout = deadline_call_timeout(timeout, stage)
        if timeout is not None:
            kwargs["timeout"] = timeout
        
        local_url = os.environ.get("BEHIVE_LLM_URL") or os.environ.get("BEHIVE_LOCAL_LLM_URL")
        if local_url and ("local" in model or "ollama" in model):
            kwargs["api_base"] = local_url
            kwargs["api_key"] = "not-needed"
            model_name = model.split("/")[-1] if "/" in model else model
            kwargs["model"] = f"openai/{model_name}"
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        if strict:
            _strict_completion_options(litellm, kwargs)
            import asyncio as _busy_asyncio
            busy = _BridgeBusyWait()
            while True:
                try:
                    response = await litellm.acompletion(**kwargs)
                    return response.choices[0].message.content or ""
                except Exception as exc:
                    delay = busy.delay(exc)
                    if delay is None:
                        if busy.deadline_reached:
                            raise PhaseDeadlineExceeded("bridge busy until the phase deadline") from None
                        raise
                    await _busy_asyncio.sleep(delay)
                    busy.prepare_retry(kwargs, timeout, stage)

        response = await litellm.acompletion(**kwargs)
        return response.choices[0].message.content or ""
        
    except ImportError:
        if strict:
            raise
        import asyncio
        return await asyncio.to_thread(complete, prompt, stage, system, max_tokens, temperature, json_mode)
    except PhaseDeadlineExceeded:
        raise  # callers defer the work; an empty answer would look like a real one
    except Exception as e:
        if is_deadline_timeout(e):
            raise PhaseDeadlineExceeded("model call cut by the phase deadline") from None
        if strict:
            raise
        log.error("Async LLM call failed (model=%s, error_type=%s)", model, type(e).__name__)
        return ""


# ─── Direct API fallback (no litellm) ──────────────────────────────────────

def _direct_call(
    model: str,
    prompt: str,
    system: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Direct API call without litellm. Limited to Anthropic, OpenAI, Bedrock."""
    
    if "anthropic" in model and os.environ.get("ANTHROPIC_API_KEY"):
        return _call_anthropic(model, prompt, system, max_tokens, temperature)
    elif "openai" in model or "gpt" in model:
        return _call_openai(model, prompt, system, max_tokens, temperature)
    elif "bedrock" in model or _has_aws_credentials():
        return _call_bedrock(model, prompt, system, max_tokens, temperature)
    else:
        raise RuntimeError(
            f"Cannot call model '{model}' without litellm. "
            "Install: pip install litellm"
        )


def _call_anthropic(model: str, prompt: str, system: str, max_tokens: int, temperature: float) -> str:
    """Direct Anthropic API call."""
    import httpx
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    
    # Extract model name (strip provider prefix)
    model_name = model.split("/")[-1] if "/" in model else model
    
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system or "You are a research assistant.",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=deadline_call_timeout(120.0),  # capped by the phase deadline
    )
    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"]


def _call_openai(model: str, prompt: str, system: str, max_tokens: int, temperature: float) -> str:
    """Direct OpenAI API call."""
    import httpx
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    
    # Extract model name
    model_name = model.split("/")[-1] if "/" in model else model
    
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=deadline_call_timeout(120.0),  # capped by the phase deadline
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _call_bedrock(model: str, prompt: str, system: str, max_tokens: int, temperature: float) -> str:
    """Direct AWS Bedrock call."""
    import boto3
    
    model_id = model.replace("bedrock/", "") if model.startswith("bedrock/") else model
    
    # If model_id still has provider prefix or looks wrong, use inference profile
    if "/" in model_id and not model_id.startswith(("us.", "eu.", "global.")):
        # e.g. "anthropic/claude-sonnet-4-20250514" → need Bedrock inference profile
        model_id = f"us.{model_id.replace('/', '.')}-v1:0"
    
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("bedrock-runtime", region_name=region)
    
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system or "You are a research assistant.",
        "messages": [{"role": "user", "content": prompt}],
    })
    
    resp = client.invoke_model(
        body=body,
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
    )
    
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"]


# ─── Embedding Support ─────────────────────────────────────────────────────────

_EMBED_MODEL = os.environ.get("BEHIVE_EMBED_MODEL", "")


def embed(texts: list[str], model: str = "") -> list[list[float]]:
    """
    Generate embeddings for a list of texts.
    
    BYOK routing:
    1. BEHIVE_EMBED_MODEL env → litellm.embedding()
    2. Auto-detect: if OPENAI_API_KEY → text-embedding-3-small
    3. If AWS creds → amazon.titan-embed-text-v2:0
    4. sentence-transformers local fallback (if installed)
    
    Returns list of float vectors (one per input text).
    """
    embed_model = model or _EMBED_MODEL or _detect_embed_model()
    
    if not embed_model:
        raise RuntimeError(
            "No embedding model configured. Set BEHIVE_EMBED_MODEL env var, "
            "or provide OPENAI_API_KEY / AWS credentials."
        )
    
    # Route through litellm for all cloud providers
    if embed_model.startswith("local/"):
        return _local_embed(texts, embed_model.replace("local/", ""))
    
    try:
        import litellm
        response = litellm.embedding(model=embed_model, input=texts)
        return [item["embedding"] for item in response.data]
    except Exception as e:
        log.warning("litellm embedding failed (%s), trying local fallback: %s", embed_model, e)
        return _local_embed(texts)


def _detect_embed_model() -> str:
    """Auto-detect best available embedding model."""
    if os.environ.get("OPENAI_API_KEY"):
        return "text-embedding-3-small"
    if os.environ.get("VOYAGE_API_KEY"):
        return "voyage/voyage-3-lite"
    if _has_aws_credentials():
        return "bedrock/amazon.titan-embed-text-v2:0"
    # Check for local sentence-transformers
    try:
        import sentence_transformers  # noqa: F401
        return "local/all-MiniLM-L6-v2"
    except ImportError:
        pass
    return ""


def _local_embed(texts: list[str], model_name: str = "all-MiniLM-L6-v2") -> list[list[float]]:
    """Fallback: local sentence-transformers embedding."""
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(model_name)
        embeddings = _model.encode(texts, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]
    except ImportError:
        raise RuntimeError(
            "No embedding provider available. Install sentence-transformers "
            "(`pip install sentence-transformers`) or set OPENAI_API_KEY / AWS creds."
        )
