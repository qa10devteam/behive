"""
Unified LLM abstraction for BeHive engine.

All pipeline stages call through this module. It respects:
1. `behive config` settings (per-stage model routing)
2. Environment variables: OPENAI_API_KEY, ANTHROPIC_API_KEY, AWS credentials
3. Fallback chain: configured model → litellm → error with clear message

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

# ─── Configuration ──────────────────────────────────────────────────────────

def _get_configured_model(stage: str) -> str:
    """Get model for a pipeline stage from behive config."""
    try:
        from behive.config import get_model_for_stage
        return get_model_for_stage(stage)
    except (ImportError, Exception):
        pass
    
    # Fallback: check env vars
    env_model = os.environ.get(f"BEHIVE_MODEL_{stage.upper()}")
    if env_model:
        return env_model
    
    # Global fallback
    env_model = os.environ.get("BEHIVE_MODEL")
    if env_model:
        return env_model
    
    # Auto-detect from available API keys
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic/claude-sonnet-4-20250514"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai/gpt-4o"
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"
    
    # Check ~/.aws/credentials file
    aws_creds = os.path.expanduser("~/.aws/credentials")
    if os.path.exists(aws_creds):
        return "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"
    
    # Check for AWS instance role (EC2/ECS)
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials()
        if creds is not None:
            return "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"
    except Exception:
        pass
    
    # Local LLM fallback
    local_url = os.environ.get("BEHIVE_LOCAL_LLM_URL") or os.environ.get("BEHIVE_SGLANG_URL")
    if local_url:
        return "openai/local"  # litellm uses openai compat for local
    
    return ""


def _check_keys() -> str:
    """Return error message if no LLM credentials are configured."""
    if any([
        os.environ.get("ANTHROPIC_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("AWS_ACCESS_KEY_ID"),
        os.environ.get("AWS_PROFILE"),
        os.environ.get("BEHIVE_LOCAL_LLM_URL"),
        os.environ.get("BEHIVE_SGLANG_URL"),
    ]):
        return ""
    
    # Check for AWS credentials file
    aws_creds = os.path.expanduser("~/.aws/credentials")
    if os.path.exists(aws_creds):
        return ""
    
    # Check for AWS instance role (EC2/ECS)
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials()
        if creds is not None:
            return ""
    except Exception:
        pass
    
    return (
        "No LLM API key configured. BeHive uses YOUR OWN LLM subscription.\n"
        "Set one of:\n"
        "  export ANTHROPIC_API_KEY=your-key-here\n"
        "  export OPENAI_API_KEY=your-key-here\n"
        "  export AWS_ACCESS_KEY_ID=... (for Bedrock)\n"
        "  export BEHIVE_LOCAL_LLM_URL=http://localhost:11434/v1 (for Ollama/vLLM)\n"
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
    Synchronous LLM completion. Routes to configured model for the given stage.
    
    Args:
        prompt: User message / main prompt
        stage: Pipeline stage (scout, harvest, process, synth) — determines model
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
            "Run: behive config --preset balanced"
        )
    
    log.debug(f"LLM call: stage={stage} model={model} tokens={max_tokens}")
    
    # Try litellm first (supports 100+ providers)
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
        
        # Handle local LLM URL
        if model == "openai/local" or model.startswith("ollama"):
            local_url = (
                os.environ.get("BEHIVE_LOCAL_LLM_URL") or 
                os.environ.get("BEHIVE_SGLANG_URL") or
                "http://localhost:11434/v1"
            )
            kwargs["api_base"] = local_url
            kwargs["api_key"] = "not-needed"
            # Strip prefix for local
            if model == "openai/local":
                kwargs["model"] = "local-model"
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        response = litellm.completion(**kwargs)
        return response.choices[0].message.content or ""
        
    except ImportError:
        log.warning("litellm not installed. Falling back to direct API calls.")
    except Exception as e:
        log.error(f"litellm call failed: {e}")
        # Fall through to direct calls
    
    # Fallback: direct API calls without litellm
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
        
        if model == "openai/local" or model.startswith("ollama"):
            local_url = (
                os.environ.get("BEHIVE_LOCAL_LLM_URL") or
                os.environ.get("BEHIVE_SGLANG_URL") or
                "http://localhost:11434/v1"
            )
            kwargs["api_base"] = local_url
            kwargs["api_key"] = "not-needed"
            if model == "openai/local":
                kwargs["model"] = "local-model"
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        response = await litellm.acompletion(**kwargs)
        return response.choices[0].message.content or ""
        
    except ImportError:
        # Sync fallback
        import asyncio
        return await asyncio.to_thread(complete, prompt, stage, system, max_tokens, temperature, json_mode)
    except Exception as e:
        log.error(f"Async LLM call failed: {e}")
        return ""


# ─── Direct API fallback (no litellm) ──────────────────────────────────────

def _direct_call(
    model: str,
    prompt: str,
    system: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Direct API call without litellm. Supports OpenAI, Anthropic, Bedrock."""
    
    # If model says anthropic but no API key → try Bedrock
    if "anthropic" in model and "bedrock" not in model:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return _call_anthropic(prompt, system, max_tokens, temperature)
        else:
            # Fallback to Bedrock for Anthropic models
            return _call_bedrock(model, prompt, system, max_tokens, temperature)
    elif "openai" in model or "gpt" in model:
        return _call_openai(model, prompt, system, max_tokens, temperature)
    elif "bedrock" in model or os.environ.get("AWS_ACCESS_KEY_ID"):
        return _call_bedrock(model, prompt, system, max_tokens, temperature)
    else:
        # Last resort — try Bedrock (instance role)
        try:
            return _call_bedrock(model, prompt, system, max_tokens, temperature)
        except Exception:
            raise RuntimeError(
                f"Cannot call model '{model}' without litellm installed. "
                "Install: pip install litellm"
            )


def _call_anthropic(prompt: str, system: str, max_tokens: int, temperature: float) -> str:
    """Direct Anthropic API call."""
    import httpx
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
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
    
    # Strip provider prefix
    model_name = model.split("/")[-1] if "/" in model else model
    if model_name == "local":
        model_name = "gpt-4o"
    
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
    
    # Map model names to Bedrock model IDs
    model_id = model.replace("bedrock/", "") if model.startswith("bedrock/") else model
    
    # Convert anthropic/ or plain model names to Bedrock format
    BEDROCK_MAP = {
        "anthropic/claude-sonnet-4-20250514": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "anthropic/claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic/claude-3-5-haiku-20241022": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic/claude-3-5-sonnet-20241022": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "claude-sonnet-4-20250514": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    model_id = BEDROCK_MAP.get(model_id, model_id)
    
    if not model_id or "/" in model_id:
        model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    
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
