# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

- **Email:** hello@qa10.io
- **Subject:** [SECURITY] BeHive vulnerability report

Do NOT open a public issue for security vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Current |

## Security Considerations

- BeHive's stealth drones respect robots.txt by default
- API keys are never stored in claims or reports
- The MCP server binds to localhost by default
- Docker containers run as non-root user
