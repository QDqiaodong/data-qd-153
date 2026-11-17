DESCRIPTION = 'Measured-state downtime history on a fixed fault tree'
import argparse, json, sys
from pathlib import Path
from . import store

def _horizon(text):
    try: value = int(text)
    except ValueError: raise argparse.ArgumentTypeError('horizon must be an integer number of seconds')
    if not 1 <= value <= store.MAX_HORIZON:
        raise argparse.ArgumentTypeError('horizon must be within 1..7776000 seconds')
    return value

def _day(text):
    try: value = int(text)
    except ValueError: raise argparse.ArgumentTypeError('day must be an integer index')
    if value < 0: raise argparse.ArgumentTypeError('day must be non-negative')
    return value

def main(argv=None):
    parser = argparse.ArgumentParser(prog='fault-history', description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='create a dataset bound to a fault tree and a fixed horizon')
    init.add_argument('--tree', type=Path, required=True)
    init.add_argument('--horizon', type=_horizon, required=True)
    init.add_argument('--dataset', type=Path, required=True)
    apply_ = commands.add_parser('apply', help='settle one batch of measured-state revisions')
    apply_.add_argument('--dataset', type=Path, required=True)
    apply_.add_argument('--source', type=Path, required=True)
    query = commands.add_parser('query', help='read one pinned generation snapshot for a day')
    query.add_argument('--dataset', type=Path, required=True)
    query.add_argument('--day', type=_day, required=True)
    query.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init': report = store.init(args.tree, args.horizon, args.dataset)
        elif args.command == 'apply': report = store.apply(args.dataset, args.source)
        else: report = store.query(args.dataset, args.day, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0

if __name__ == '__main__': raise SystemExit(main())
