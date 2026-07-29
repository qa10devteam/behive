"""Allow running: python -m behive.engine.orchestrator run 'topic'"""
import sys
import os

# Install compat shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass

from behive.engine.orchestrator import main

if __name__ == "__main__":
    main()
