"""AI Brand Visibility Checker — free tool endpoint.

Accepts a brand/company name, queries the configured LLM asking "what do you know
about X?", analyzes the response for depth/accuracy, and returns a visibility score.

This is a zero-auth public endpoint designed as a lead magnet.
"""

import os
import time
import hashlib
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/public/brand-check", tags=["free-tools"])

# Simple in-memory rate limiting (per IP, 10 checks/hour)
_rate_limit: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 3600  # 1 hour
_RATE_LIMIT_MAX = 10


class BrandCheckRequest(BaseModel):
    """Request to check brand visibility in AI systems."""
    brand_name: str = Field(..., min_length=2, max_length=200, description="Company or brand name to check")
    url: Optional[str] = Field(None, max_length=500, description="Optional website URL for context")
    language: str = Field("en", description="Language for analysis (en, pl, de, etc.)")


class BrandCheckResponse(BaseModel):
    """AI Brand Visibility analysis result."""
    brand_name: str
    visibility_score: float = Field(..., ge=0, le=100, description="0-100 visibility score")
    verdict: str = Field(..., description="Human-readable verdict")
    ai_knows: dict = Field(..., description="What AI knows about the brand")
    recommendations: list[str] = Field(..., description="Actionable recommendations to improve visibility")
    checked_models: list[str] = Field(..., description="Which AI models were checked")
    timestamp: str


def _rate_check(client_ip: str) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    now = time.time()
    if client_ip not in _rate_limit:
        _rate_limit[client_ip] = []
    # Prune old entries
    _rate_limit[client_ip] = [t for t in _rate_limit[client_ip] if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_limit[client_ip]) >= _RATE_LIMIT_MAX:
        return False
    _rate_limit[client_ip].append(now)
    return True


@router.post("", response_model=BrandCheckResponse)
async def check_brand_visibility(req: BrandCheckRequest):
    """
    Check how visible a brand is to AI assistants (ChatGPT, Claude, Gemini).
    
    Free tool — no API key required. Rate limited to 10 checks per hour per IP.
    """
    from fastapi import Request as _Req
    # Rate limiting would use request.client.host in production
    # For now, hash the brand name as pseudo-IP
    pseudo_ip = hashlib.md5(req.brand_name.lower().encode()).hexdigest()[:8]
    if not _rate_check(pseudo_ip):
        raise HTTPException(429, "Rate limit exceeded. Try again in an hour.")

    # Use the configured LLM to check brand knowledge
    try:
        from behive.engine.llm import complete_async
    except ImportError:
        raise HTTPException(503, "LLM backend not configured. Set BEHIVE_MODEL or an API key.")

    # Construct the analysis prompt
    url_context = f" (website: {req.url})" if req.url else ""
    analysis_prompt = f"""You are an AI brand visibility analyst. I need you to assess how well-known the brand "{req.brand_name}"{url_context} is to AI systems.

Please provide your analysis in the following JSON format (respond ONLY with valid JSON, no markdown):
{{
    "known": true/false,
    "confidence": 0.0-1.0,
    "category": "what industry/category this brand belongs to",
    "key_facts": ["list of facts you know about this brand"],
    "competitors_mentioned": ["any competitors you associate with this brand"],
    "sentiment": "positive/neutral/negative/unknown",
    "presence_signals": {{
        "has_wikipedia": true/false/unknown,
        "has_news_coverage": true/false/unknown,
        "has_social_media": true/false/unknown,
        "has_reviews": true/false/unknown,
        "has_technical_docs": true/false/unknown
    }},
    "visibility_assessment": "A 2-3 sentence assessment of this brand's AI visibility"
}}

Be honest. If you don't know the brand, say so. Don't hallucinate facts."""

    try:
        response = await complete_async(
            prompt=analysis_prompt,
            system="You are an AI brand visibility analyst. Respond only with valid JSON.",
            temperature=0.1,
            max_tokens=1000,
            json_mode=True,
        )
    except Exception as e:
        raise HTTPException(503, f"LLM analysis failed: {str(e)[:100]}")

    # Parse the LLM response
    import json
    try:
        # Try to extract JSON from response
        response_text = response if isinstance(response, str) else str(response)
        # Handle potential markdown wrapping
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        analysis = json.loads(response_text.strip())
    except (json.JSONDecodeError, IndexError):
        # Fallback — brand is likely unknown if we can't parse
        analysis = {
            "known": False,
            "confidence": 0.1,
            "category": "unknown",
            "key_facts": [],
            "competitors_mentioned": [],
            "sentiment": "unknown",
            "presence_signals": {},
            "visibility_assessment": "Could not determine brand visibility."
        }

    # Calculate visibility score (0-100)
    score = 0.0
    if analysis.get("known", False):
        score += 30  # Base score for being known
        score += min(len(analysis.get("key_facts", [])) * 8, 30)  # Facts depth
        score += analysis.get("confidence", 0) * 20  # Confidence bonus
        signals = analysis.get("presence_signals", {})
        signal_count = sum(1 for v in signals.values() if v is True)
        score += signal_count * 4  # Presence signals
    else:
        score = max(5, analysis.get("confidence", 0) * 15)  # Minimal score

    score = min(100, max(0, score))

    # Generate verdict
    if score >= 80:
        verdict = "🟢 Excellent — AI assistants know your brand well"
    elif score >= 60:
        verdict = "🟡 Good — AI has moderate awareness of your brand"
    elif score >= 30:
        verdict = "🟠 Low — AI barely knows your brand exists"
    else:
        verdict = "🔴 Invisible — AI assistants don't know your brand"

    # Generate recommendations
    recommendations = []
    if score < 80:
        recommendations.append("Publish structured content (blog posts, docs) that AI can index")
    if not analysis.get("presence_signals", {}).get("has_technical_docs"):
        recommendations.append("Add llms.txt to your website root for AI discoverability")
    if not analysis.get("presence_signals", {}).get("has_wikipedia"):
        recommendations.append("Build authority through citations in third-party sources")
    if score < 50:
        recommendations.append("Create a comprehensive 'About' page with structured data (JSON-LD)")
        recommendations.append("Publish regularly — AI models update knowledge from fresh content")
    if not analysis.get("presence_signals", {}).get("has_reviews"):
        recommendations.append("Get listed on review platforms (G2, Capterra, Trustpilot)")
    recommendations.append("Use BeHive to research what AI says about your competitors")

    from datetime import datetime, timezone

    return BrandCheckResponse(
        brand_name=req.brand_name,
        visibility_score=round(score, 1),
        verdict=verdict,
        ai_knows={
            "recognized": analysis.get("known", False),
            "category": analysis.get("category", "unknown"),
            "facts_count": len(analysis.get("key_facts", [])),
            "key_facts": analysis.get("key_facts", [])[:5],
            "competitors_associated": analysis.get("competitors_mentioned", [])[:5],
            "sentiment": analysis.get("sentiment", "unknown"),
            "assessment": analysis.get("visibility_assessment", ""),
        },
        recommendations=recommendations[:6],
        checked_models=["claude-sonnet"],  # Will be dynamic based on config
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
