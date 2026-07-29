"""
Legacy Bedrock client compatibility layer.

Provides a drop-in replacement for boto3 bedrock-runtime client
that routes through behive.engine.llm (litellm/Anthropic/OpenAI/Bedrock).

Usage in existing engine code:
    # OLD: client = boto3.client('bedrock-runtime')
    # NEW: client = get_bedrock_compat_client(stage="scout")
    
    # This still works exactly the same:
    resp = client.invoke_model(
        modelId="...",
        body=json.dumps({...messages...})
    )
    text = json.loads(resp['body'].read())['content'][0]['text']
"""
import json
import io
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)


class BedrockCompatResponse:
    """Mimics boto3 Bedrock response format."""
    
    def __init__(self, text: str):
        self._body = json.dumps({
            "content": [{"type": "text", "text": text}],
            "model": "behive-routed",
            "stop_reason": "end_turn",
        }).encode()
    
    def read(self) -> bytes:
        return self._body


class BedrockCompatClient:
    """
    Drop-in replacement for boto3.client('bedrock-runtime').
    Routes all invoke_model() calls through behive.engine.llm.
    """
    
    def __init__(self, stage: str = "process"):
        self.stage = stage
    
    def invoke_model(
        self,
        body: str = "",
        modelId: str = "",
        contentType: str = "application/json",
        accept: str = "application/json",
        **kwargs,
    ) -> dict:
        """
        Compatible invoke_model that routes through llm.complete().
        
        Parses the Bedrock-format body and calls the unified LLM layer.
        Returns a response in the same format boto3 would return.
        """
        from behive.engine.llm import complete
        
        # Parse the request body
        req = json.loads(body) if isinstance(body, str) else json.loads(body.decode()) if isinstance(body, bytes) else body
        
        messages = req.get("messages", [])
        system = req.get("system", "")
        max_tokens = req.get("max_tokens", 4096)
        temperature = req.get("temperature", 0.3)
        
        # Extract prompt from messages
        prompt = ""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Multi-part message
                    prompt = " ".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                else:
                    prompt = content
        
        if not prompt:
            log.warning("invoke_model called with empty prompt")
            return {"body": BedrockCompatResponse("")}
        
        try:
            text = complete(
                prompt=prompt,
                stage=self.stage,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            log.error(f"LLM call failed in compat layer: {e}")
            text = ""
        
        return {"body": BedrockCompatResponse(text)}


def get_bedrock_compat_client(stage: str = "process") -> BedrockCompatClient:
    """
    Get a Bedrock-compatible client that routes through the unified LLM layer.
    
    This is the RECOMMENDED way to get a client in engine modules.
    It works with ANY configured LLM provider, not just AWS Bedrock.
    """
    return BedrockCompatClient(stage=stage)


# ─── Monkey-patch for existing boto3.client calls ────────────────────────────

_original_boto3_client = None

def patch_boto3_bedrock(stage: str = "process") -> None:
    """
    Monkey-patch boto3.client so that boto3.client('bedrock-runtime')
    returns our compat client instead of the real Bedrock client.
    
    Call this early in pipeline startup.
    """
    global _original_boto3_client
    
    try:
        import boto3
        _original_boto3_client = boto3.client
        
        def patched_client(service_name, *args, **kwargs):
            if service_name == "bedrock-runtime":
                log.debug(f"Intercepted bedrock-runtime client → BedrockCompatClient(stage={stage})")
                return BedrockCompatClient(stage=stage)
            return _original_boto3_client(service_name, *args, **kwargs)
        
        boto3.client = patched_client
        log.debug("boto3.client patched for Bedrock compatibility")
    except ImportError:
        # boto3 not installed — that's fine, users don't need it
        log.debug("boto3 not available, no patching needed")


def unpatch_boto3() -> None:
    """Restore original boto3.client."""
    global _original_boto3_client
    if _original_boto3_client:
        import boto3
        boto3.client = _original_boto3_client
        _original_boto3_client = None
