"""Command-line interface for the benchmark runner."""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import subprocess
import tarfile
from .contracts import schedule,pilot_schedule,validate_plan
from .evidence import check_inputs,read
from .execution import merge,run
from .reporting import make_report
from . import studies, archives, workflow, lab, calibration

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
            p.add_argument("--resume", action="store_true")
            p.add_argument("--max-trials", type=int)
            selection = p.add_mutually_exclusive_group()
            selection.add_argument("--trial-ids", nargs="+")
            selection.add_argument("--pilot", action="store_true")
    sub.add_parser("report").add_argument("directory", type=Path)
    p = sub.add_parser("merge")
    p.add_argument("directories", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    study = sub.add_parser("study").add_subparsers(dest="study_action", required=True)
    for name in ("prepare", "run", "check", "collect"):
        p = study.add_parser(name)
        p.add_argument("profile", type=Path)
        p.add_argument("--lab", required=True, type=Path)
        p.add_argument("--output", required=True, type=Path)
        if name in ("run","collect"): p.add_argument("--max-trials", type=int)
    for name in ("resume", "status", "report", "archive"):
        p = study.add_parser(name)
        p.add_argument("directory", type=Path)
        if name == "resume": p.add_argument("--max-trials", type=int)
        if name == "archive": p.add_argument("--output", required=True, type=Path)
    study.add_parser("verify-archive").add_argument("archive", type=Path)
    lab_commands=sub.add_parser('lab').add_subparsers(dest='lab_action',required=True)
    p=lab_commands.add_parser('doctor');p.add_argument('profile',type=Path)
    p.add_argument('--lab',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p=lab_commands.add_parser('calibrate');p.add_argument('profile',type=Path);p.add_argument('--output',required=True,type=Path)
    p=lab_commands.add_parser('calibration-report');p.add_argument('directory',type=Path);p.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    try:
        if args.action=='lab' and args.lab_action=='calibration-report':
            print(json.dumps(calibration.report(args.directory.resolve(),args.output.resolve())));return 0
        if args.action=='lab' and args.lab_action=='calibrate':
            result=calibration.run(args.profile.resolve(),args.output.resolve());print(json.dumps(result));return 0 if result['cleanup_ok'] else 2
        if args.action=='lab':
            print(json.dumps(lab.doctor(args.profile.resolve(),args.lab.resolve(),args.output.resolve())));return 0
        if args.action == "study":
            action = args.study_action
            if action=='collect':return workflow.start(args.profile.resolve(),args.lab.resolve(),args.output.resolve(),args.max_trials)
            is_workflow=hasattr(args,'directory') and (args.directory/'workflow.json').exists()
            if is_workflow and action=='resume':return workflow.resume(args.directory.resolve(),args.max_trials)
            if is_workflow and action=='status':
                print(json.dumps(lab.progress(workflow.status(args.directory.resolve()))));return 0
            if is_workflow and action=='report':
                workflow.status(args.directory.resolve());args.directory=args.directory/'collection'
            if action == "check":
                result = studies.check(args.profile.resolve(), args.lab.resolve(), args.output.resolve())
            elif action in ("prepare", "run"):
                result = studies.prepare(args.profile.resolve(), args.lab.resolve(), args.output.resolve())
                if action == "run": result = studies.execute(args.output.resolve(), args.max_trials)
            elif action == "resume": result = studies.execute(args.directory.resolve(), args.max_trials)
            elif action == "status": result = studies.status(args.directory.resolve())
            elif action == "report": result = {"report": str(studies.report(args.directory.resolve()))}
            elif action == "archive":
                with (args.directory/('.workflow.lock' if is_workflow else '.study.lock')).open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    (workflow.status if is_workflow else studies.status)(args.directory.resolve())
                    result = archives.create(args.directory.resolve(), args.output.resolve())
            else: result = archives.verify(args.archive.resolve())
            if action in ('run','resume') and result.get('complete') and args.max_trials is None:
                directory=args.output if action=='run' else args.directory
                result['report']=str(studies.report(directory.resolve()))
            print(json.dumps(result))
            return 2 if result.get("failed") or result.get("check_passed") is False else 0
        if args.action == "validate":
            plan = validate_plan(read(args.plan))
            check_inputs(plan)
            print(json.dumps({"status": "ready", "trials": len(schedule(plan))}))
            return 0
        if args.action == "run":
            if args.pilot:
                args.trial_ids = [r['id'] for r in pilot_schedule(validate_plan(read(args.plan)))]
            report = run(args.plan.resolve(), args.output.resolve(), args.shard_index, args.shard_count, resume=args.resume, max_trials=args.max_trials, trial_ids=args.trial_ids)
        elif args.action == "merge":
            report = merge([d.resolve() for d in args.directories], args.output.resolve())
        else:
            report = make_report(args.directory.resolve())
        print(json.dumps({"status": report["status"], "trial_count": report["trial_count"]}))
        return 2 if report["status"] == "incomplete" else 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError, subprocess.SubprocessError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2
