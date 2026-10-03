"""Command-line interface for the benchmark runner."""
import argparse
import json
from pathlib import Path
import sys
from .contracts import schedule,validate_plan
from .evidence import check_inputs,read
from .execution import merge,run
from .reporting import make_report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "validate"):
        p = sub.add_parser(name)
        p.add_argument("plan", type=Path)
        if name == "run":
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("--shard-index", type=int, default=0)
            p.add_argument("--shard-count", type=int, default=1)
    sub.add_parser("report").add_argument("directory", type=Path)
    p = sub.add_parser("merge")
    p.add_argument("directories", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "validate":
            plan = validate_plan(read(args.plan))
            check_inputs(plan)
            print(json.dumps({"status": "ready", "trials": len(schedule(plan))}))
            return 0
        if args.action == "run":
            report = run(args.plan.resolve(), args.output.resolve(), args.shard_index, args.shard_count)
        elif args.action == "merge":
            report = merge([d.resolve() for d in args.directories], args.output.resolve())
        else:
            report = make_report(args.directory.resolve())
        print(json.dumps({"status": report["status"], "trial_count": report["trial_count"]}))
        return 2 if report["status"] == "incomplete" else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2
