# Federated Knowledge Network (FKN) — Design Document

## Concept

BabyLoveGrowth's "Backlink Exchange Network" (3000+ sites exchanging links) creates a powerful network effect — more users → better links for everyone. But it's ethically questionable (Google can penalize link schemes) and low-value (just SEO manipulation).

**Our adaptation**: Instead of exchanging *links*, BeHive users exchange *intelligence*. Each participant contributes verified knowledge back to a shared pool. More researchers → richer knowledge graph → better results for everyone.

This is **Collective Intelligence as a Service**.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│              Federated Knowledge Hub              │
│         (BeHive Cloud / Self-Hosted Node)         │
│                                                   │
│  ┌───────────────┐  ┌───────────────┐           │
│  │ Shared Claims │  │ Entity Graph  │           │
│  │   (verified)  │  │  (connected)  │           │
│  └───────────────┘  └───────────────┘           │
│                                                   │
│  ┌───────────────┐  ┌───────────────┐           │
│  │  Source Trust │  │  Contributor  │           │
│  │    Scores     │  │   Reputation  │           │
│  └───────────────┘  └───────────────┘           │
└──────────┬───────────────────┬───────────────────┘
           │                   │
    ┌──────┴──────┐     ┌──────┴──────┐
    │  Instance A │     │  Instance B │
    │  (Company X)│     │  (Company Y)│
    │             │     │             │
    │  Private    │     │  Private    │
    │  missions   │     │  missions   │
    │  + local DB │     │  + local DB │
    └─────────────┘     └─────────────┘
```

---

## Key Principles

### 1. Opt-in Contribution
Users explicitly choose which mission results to contribute.
- Default: private (nothing leaves your instance)
- Opt-in per mission: "Contribute findings to the network"
- Opt-in globally: "Auto-contribute all non-sensitive research"

### 2. Privacy by Design
- Only **verified claims** are shared (not raw content, not queries)
- Source URLs are shared (public info), but internal documents/notes never leave
- Claims are anonymized — you can't tell WHO researched a topic
- API key / organization identity is hashed for contribution tracking

### 3. Quality Gating
Not all claims enter the shared pool:
- Minimum confidence threshold: 0.7
- Must have at least 1 verifiable source URL
- Flagged as garbage = never shared
- Duplicate detection (dedup against existing claims)

### 4. Contributor Reputation
- Each contributor earns reputation based on claim quality
- Higher-reputation contributors' claims get priority in results
- Bad claims (flagged by others) decrease reputation
- Reputation is per-domain/topic (you can be expert in "construction" but not "biotech")

### 5. Network Effect Value
Every user gets:
- **Pre-researched knowledge** — if someone already researched your topic, you get instant claims
- **Source discovery** — URLs that others found but you might miss
- **Entity enrichment** — relationships discovered by the collective
- **Confidence boosting** — claim X confirmed by 5 independent researchers = higher confidence
- **Contradiction alerts** — if your research contradicts the network, you get flagged for review

---

## Data Model

### Shared Claim (what enters the network)
```python
class FederatedClaim(BaseModel):
    claim_hash: str           # SHA256 of normalized claim text
    text: str                 # The claim itself
    confidence: float         # Original confidence score
    network_confidence: float # Boosted by confirmations
    source_urls: list[str]    # Public source URLs
    domain_tags: list[str]    # Topic domains (auto-classified)
    entity_refs: list[str]    # Entity names mentioned
    contributor_hash: str     # Anonymized contributor ID
    contributed_at: datetime
    confirmations: int        # How many independent instances confirmed this
    contradictions: int       # How many found counter-evidence
```

### Contributor Profile (anonymous)
```python
class FederatedContributor(BaseModel):
    contributor_hash: str     # Hash of API key
    reputation_score: float   # 0.0 - 1.0
    claims_contributed: int
    claims_confirmed: int     # Their claims confirmed by others
    claims_flagged: int       # Their claims contradicted
    domain_expertise: dict[str, float]  # domain → expertise score
    joined_at: datetime
```

### Network Query (how instances query the hub)
```python
class NetworkQuery(BaseModel):
    query_embedding: list[float]  # Semantic search
    domain_filter: list[str] | None
    min_confidence: float = 0.6
    min_confirmations: int = 0
    limit: int = 50
```

---

## API Endpoints (Hub)

```
POST /federation/contribute
  Body: {claims: [...], contributor_token: "hash"}
  → Adds verified claims to the shared pool

GET /federation/search?q=...&domain=...&min_conf=0.7
  → Returns matching claims from the network
  → Requires contributor_token (must contribute to consume)

POST /federation/confirm
  Body: {claim_hash: "...", confirmed: true/false}
  → Vote on a claim (confirmation or contradiction)

GET /federation/stats
  → Network-wide stats (total claims, contributors, domains)

GET /federation/profile/{contributor_hash}
  → Contributor reputation and expertise
```

---

## Integration with BeHive Engine

### On Mission Complete (if opted in)
```python
async def contribute_to_network(mission_id: str):
    """After a mission completes, optionally share findings."""
    claims = get_mission_claims(mission_id)
    
    # Filter: only share high-quality, non-garbage claims
    shareable = [
        c for c in claims
        if c.confidence >= 0.7
        and not c.is_garbage
        and c.source_urls  # must have verifiable sources
    ]
    
    if not shareable:
        return
    
    # Anonymize and push
    payload = [FederatedClaim.from_local(c) for c in shareable]
    await hub_client.contribute(payload)
```

### On Research Start (pre-scout enrichment)
```python
async def enrich_from_network(query: str, domain: str = None):
    """Before scouting, check if the network already has relevant knowledge."""
    existing = await hub_client.search(query, domain=domain, min_conf=0.7)
    
    if existing:
        # Inject as pre-verified claims (skip re-harvesting these)
        for claim in existing:
            inject_pre_verified_claim(claim)
        
        # Also use their source URLs as seed URLs for scouting
        seed_urls = set()
        for claim in existing:
            seed_urls.update(claim.source_urls)
        add_to_scout_queue(seed_urls)
```

---

## Economics / Incentive Design

### "Give to Get" Model
- You must contribute ≥1 mission/month to query the network
- More contributions → higher query limits
- Free tier: 50 network queries/month (with ≥5 contributed claims)
- Paid tier: unlimited queries (part of Scale/Enterprise plan)

### Trust Without Gaming
- Claims are verified against sources (source URL must resolve)
- Spam detection: same claim from same contributor = no reputation gain
- Sybil resistance: new contributors start with 0.5 reputation, need 10+ confirmed claims to reach 0.8+
- Decay: claims older than 12 months lose confidence unless re-confirmed

---

## Competitive Moat

| BabyLoveGrowth | BeHive FKN |
|---------------|------------|
| Exchanges backlinks (SEO manipulation) | Exchanges knowledge (genuine value) |
| Google can penalize | Google rewards authoritative content |
| Zero-sum (your link = my link) | Positive-sum (my research helps you) |
| Only helps SEO | Helps understanding, decision-making, accuracy |
| Closed network | Open protocol (self-host nodes can federate) |
| Incentive: gaming Google | Incentive: building collective intelligence |

---

## Implementation Phases

### Phase 1: Hub Infrastructure (this sprint)
- [ ] Database schema for federated claims + contributors
- [ ] POST /federation/contribute endpoint
- [ ] GET /federation/search endpoint  
- [ ] Contributor token generation + reputation tracking

### Phase 2: Engine Integration
- [ ] Opt-in UI in mission creation
- [ ] Auto-contribute on mission complete
- [ ] Pre-scout network enrichment
- [ ] Confirmation/contradiction voting

### Phase 3: Incentives + Growth
- [ ] "Give to Get" enforcement
- [ ] Domain expertise leaderboards
- [ ] Network stats dashboard
- [ ] Cross-instance entity graph merging

### Phase 4: Federation Protocol
- [ ] Self-hosted nodes can run their own hub
- [ ] Hub-to-hub sync protocol (like ActivityPub for knowledge)
- [ ] Enterprise: private federated clusters (company-internal knowledge sharing)
- [ ] Public API for third-party integrations
