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
from typing import Optional

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
    "bedrock": "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0",
    "local": "openai/local-model",
}

# API key env vars → provider name
_KEY_TO_PROVIDER = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GROQ_API_KEY": "groq",
    "TOGETHER_API_KEY": "together",
    "MISTRAL_API_KEY": "mistral",
    "DEEPSEEK_API_KEY": "deepseek",
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
        Generated text (str). Empty string on failure.
    """
    error = _check_keys()
    if error:
        raise RuntimeError(error)
    
    model = _get_configured_model(stage)
    if not model:
        raise RuntimeError(
            f"No model configured for stage '{stage}'. "
            "Set BEHIVE_MODEL or an API key env var."
        )
    
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
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
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
        
        # Retry with exponential backoff (handles rate limits, transient failures)
        last_error = None
        for attempt in range(3):
            try:
                response = litellm.completion(**kwargs)
                return response.choices[0].message.content or ""
            except Exception as retry_err:
                last_error = retry_err
                err_str = str(retry_err).lower()
                # Don't retry on auth errors or invalid model
                if any(k in err_str for k in ('auth', 'invalid_api_key', 'permission', 'not found')):
                    raise
                # Retry on rate limit, timeout, server error
                if attempt < 2:
                    wait = (attempt + 1) * 2  # 2s, 4s
                    log.debug(f"LLM retry {attempt+1}/3 after {wait}s: {retry_err}")
                    import time as _time
                    _time.sleep(wait)
        
        # All retries exhausted
        if last_error:
            raise last_error
        
    except ImportError:
        log.warning("litellm not installed. Falling back to direct API calls.")
        log.warning("Install litellm for 100+ provider support: pip install litellm")
    except Exception as e:
        err_str = str(e).lower()
        # Auth errors should propagate (user needs to fix their key)
        if any(k in err_str for k in ('auth', 'invalid_api_key', 'permission', 'invalid api key', 'api_key')):
            raise
        log.error(f"LLM call failed (model={model}): {e}")
        # Fall through to direct calls as last resort
    
    # Fallback: direct API calls (supports Anthropic, OpenAI, Bedrock only)
    return _direct_call(model, prompt, system, max_tokens, temperature)


async def complete_async(
    prompt: str,
    stage: str = "process",
    system: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """Async version of complete(). Uses litellm async or falls back to sync."""
    error = _check_keys()
    if error:
        raise RuntimeError(error)
    
    model = _get_configured_model(stage)
    if not model:
        raise RuntimeError(f"No model configured for stage '{stage}'.")
    
    log.debug(f"LLM async call: stage={stage} model={model}")
    
    try:
        import litellm
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        local_url = os.environ.get("BEHIVE_LLM_URL") or os.environ.get("BEHIVE_LOCAL_LLM_URL")
        if local_url and ("local" in model or "ollama" in model):
            kwargs["api_base"] = local_url
            kwargs["api_key"] = "not-needed"
            model_name = model.split("/")[-1] if "/" in model else model
            kwargs["model"] = f"openai/{model_name}"
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        response = await litellm.acompletion(**kwargs)
        return response.choices[0].message.content or ""
        
    except ImportError:
        import asyncio
        return await asyncio.to_thread(complete, prompt, stage, system, max_tokens, temperature, json_mode)
    except Exception as e:
        log.error(f"Async LLM call failed (model={model}): {e}")
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
        timeout=120.0,
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
        timeout=120.0,
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
