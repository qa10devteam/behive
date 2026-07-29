"""Allow running: python -m behive.engine [command] 'topic' [options]"""
import sys
import os
import argparse
import logging

log = logging.getLogger(__name__)

# Install compat shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass


def main() -> None:
    """CLI entry point for running research from the command line."""
    parser = argparse.ArgumentParser(
        prog="behive.engine",
        description="BeHive research pipeline engine",
    )
    parser.add_argument("command", nargs="?", default="run",
                        help="Command: run (default)")
    parser.add_argument("topic", nargs="*",
                        help="Research topic/query")
    parser.add_argument("--mission-id", dest="mission_id", default=None,
                        help="Mission ID (auto-generated if not provided)")
    parser.add_argument("--depth", type=int, default=3,
                        help="Research depth 1-5 (default: 3)")
    parser.add_argument("--deep", action="store_true",
                        help="Enable deep research mode")
    parser.add_argument("--force", action="store_true",
                        help="Force re-run even if mission exists")
    parser.add_argument("--scale", type=int, default=200,
                        help="Max sources to scout (default: 200)")

    args = parser.parse_args()

    topic = ' '.join(args.topic) if args.topic else None

    if not topic:
        parser.print_help()
        sys.exit(1)

    # Set mission ID in env so orchestrator picks it up
    if args.mission_id:
        os.environ["BEHIVE_MISSION_ID"] = args.mission_id

    # Patch boto3.client so all invoke_model calls go through unified LLM
    try:
        from behive.engine.bedrock_compat import patch_boto3_bedrock
        patch_boto3_bedrock(stage="scout")
    except ImportError:
        pass

    from behive.engine.orchestrator import cmd_run
    cmd_run(
        topic=topic,
        deep=args.deep or args.depth >= 4,
        force=args.force,
        scale=args.scale,
    )


if __name__ == "__main__":
    main()
