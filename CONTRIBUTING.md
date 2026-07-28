# Contributing to BeHive

Thanks for your interest! BeHive is MIT-licensed and welcomes contributions.

## Development Setup

```bash
git clone https://github.com/qa10devteam/behive.git
cd behive
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

## Architecture

```
src/behive/          # pip package (client + CLI)
integrations/        # Agent skills (Hermes, OpenClaw, Claude Desktop)
docker/              # Docker setup files
```

The core pipeline code runs server-side. The `behive` PyPI package is a lightweight client that talks to the API.

## Pull Requests

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Commit with conventional messages (`feat:`, `fix:`, `docs:`)
4. Push and open a PR

## Code Style

- Python: black + isort + ruff
- Type hints everywhere
- Docstrings on public functions

## Reporting Issues

Open an issue with:
- What you expected
- What happened
- Steps to reproduce
- Your environment (OS, Python version, LLM provider)

## License

By contributing, you agree that your contributions will be licensed under MIT.
