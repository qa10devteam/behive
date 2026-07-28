# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅ Active |
| < 0.2   | ❌ Upgrade |

## Reporting a Vulnerability

**Do NOT open a public issue for security vulnerabilities.**

Email: **security@qa10.io**

Response time:
- Acknowledgment: 24 hours
- Assessment: 72 hours
- Fix (critical): 7 days

## Security Architecture

### Authentication

BeHive supports optional API key authentication:

```bash
# Set in .env to require auth on all write endpoints
BEHIVE_API_KEY=your-secure-random-key-here
```

When `BEHIVE_API_KEY` is set:
- All `/research` POST requests require `Authorization: Bearer <key>` or `X-API-Key: <key>`
- Read endpoints (`/health`, `/claims/search`) remain open
- MCP server inherits the same auth via internal API calls

When unset (default for self-hosted): all endpoints are open (suitable for localhost-only deployments behind a firewall).

### Rate Limiting

Built-in per-IP rate limiting (no external dependencies):

| Endpoint | Limit |
|----------|-------|
| General (all) | 60 requests/min |
| `/research` POST | 5 requests/min |

Rate limits use sliding window per client IP. Returns `429 Too Many Requests` with `Retry-After` header.

### Security Headers

All responses include:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

### CORS

Configurable via `BEHIVE_CORS_ORIGINS` environment variable:
```bash
# Default: allow all (for local dev)
BEHIVE_CORS_ORIGINS=*

# Production: restrict to your domains
BEHIVE_CORS_ORIGINS=https://yourdomain.com,https://app.yourdomain.com
```

### Network Security (Self-Hosted)

Recommended production setup:

```
Internet → Reverse Proxy (Caddy/nginx + TLS) → BeHive API (localhost only)
```

**Do NOT** expose BeHive directly to the internet without a reverse proxy. Use:
- Caddy with automatic HTTPS
- Or nginx with Let's Encrypt

Docker Compose binds to `127.0.0.1` by default — only local access until you configure a reverse proxy.

### Database Security

- Default connection uses `DATABASE_URL` or `BEHIVE_DB_URL` environment variable
- No hardcoded credentials in source code
- PostgreSQL: use strong passwords, restrict to localhost or Docker network
- Neo4j: change default password, bind to internal network only

### Secrets Management

**Never commit:**
- `.env` files (gitignored)
- API keys or tokens
- Database passwords
- Private keys or certificates

Use environment variables or a secrets manager (AWS Secrets Manager, Vault, etc.) in production.

### Docker Security

The Dockerfile:
- Uses multi-stage build (minimal attack surface)
- Runs as non-root user (`behive:behive`)
- Includes health checks
- No unnecessary packages

### Input Validation

- All API inputs validated via Pydantic models
- SQL queries use parameterized statements (no string interpolation)
- Research topics are sanitized before use as identifiers
- Max query length enforced

## Security Checklist for Deployers

- [ ] Set `BEHIVE_API_KEY` for any internet-facing deployment
- [ ] Use reverse proxy with TLS (Caddy recommended)
- [ ] Set strong `POSTGRES_PASSWORD` and `NEO4J_PASSWORD`
- [ ] Restrict CORS origins to your domain(s)
- [ ] Keep BeHive updated to latest version
- [ ] Monitor rate limit 429 responses for abuse
- [ ] Use firewall rules to restrict database ports (5432, 7687, 6333)
- [ ] Back up your database regularly
- [ ] Review Docker network settings (don't expose internal services)
