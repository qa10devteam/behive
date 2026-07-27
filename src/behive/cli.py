"""BeHive CLI — command-line interface for research."""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="behive",
        description="BeHive — Research engine that builds structured knowledge graphs"
    )
    subparsers = parser.add_subparsers(dest="command")
    
    # research command
    research_p = subparsers.add_parser("research", help="Run a research mission")
    research_p.add_argument("topic", help="Research topic or question")
    research_p.add_argument("-d", "--depth", type=int, default=3, help="Depth 1-5 (default: 3)")
    research_p.add_argument("-s", "--scale", type=int, help="Override task count (30-300)")
    research_p.add_argument("--json", action="store_true", help="Output JSON")
    research_p.add_argument("--no-wait", action="store_true", help="Don't wait for completion")
    
    # status command
    status_p = subparsers.add_parser("status", help="Check mission status")
    status_p.add_argument("mission_id", help="Mission ID")
    
    # version
    subparsers.add_parser("version", help="Show version")
    
    args = parser.parse_args()
    
    if args.command == "version":
        from behive import __version__
        print(f"behive {__version__}")
        return
    
    if args.command == "research":
        from behive import research
        result = asyncio.run(research(
            args.topic,
            depth=args.depth,
            scale=args.scale,
            wait=not args.no_wait,
        ))
        
        if args.json:
            import json
            print(json.dumps(result.to_dict(), indent=2, default=str))
        else:
            print(f"\n{'='*60}")
            print(f"  BeHive Research: {result.topic}")
            print(f"{'='*60}")
            print(f"  Status: {result.status}")
            print(f"  {result.summary}")
            print(f"\n  Top claims:")
            for claim in result.excellent_claims[:10]:
                print(f"    [{claim.quality_score:.2f}] {claim.text[:100]}")
            if result.report:
                print(f"\n  Report preview:")
                print(f"    {result.report[:500]}")
            print(f"{'='*60}\n")
        return
    
    parser.print_help()


if __name__ == "__main__":
    main()
