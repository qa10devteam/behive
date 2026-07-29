"""Allow running: python -m behive.engine 'topic'"""
import sys
import os
import logging

log = logging.getLogger(__name__)

# Install compat shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass

from behive.engine.orchestrator import cmd_run


def main() -> None:
    """CLI entry point for running research from the command line."""
    if len(sys.argv) < 2:
        print("Usage: python -m behive.engine 'research topic'")
        sys.exit(1)
    topic = ' '.join(sys.argv[1:])
    cmd_run(topic)


if __name__ == "__main__":
    main()
