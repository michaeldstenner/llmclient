import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="llmc",
        description="llmclient CLI — probe and diagnose LLM backends",
    )
    parser.add_argument(
        "--dir", metavar="PATH",
        help="inspect a specific data dir's queue.db and log "
             "(default: shared state queue)",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("status", help="Ollama state, connections, queue")
    sub.add_parser("queue",  help="llmclient queue state only")
    sub.add_parser("reset",  help="Reset all tripped circuit breakers")

    p_log = sub.add_parser("log", help="Show recent LLM call log entries")
    _lvl = p_log.add_mutually_exclusive_group()
    _lvl.add_argument(
        "--errors", dest="level", action="store_const", const="errors",
        help="show non-success outcomes only (default)",
    )
    _lvl.add_argument(
        "--all", dest="level", action="store_const", const="all",
        help="show all entries including successes",
    )
    p_log.set_defaults(level="errors")
    p_log.add_argument(
        "--last", type=int, default=5, metavar="N",
        help="show last N matching entries (default: 5)",
    )
    p_log.add_argument(
        "--caller", metavar="NAME",
        help="filter to a specific caller (e.g. bouncer, squirrel)",
    )
    p_log.add_argument(
        "--json", action="store_true",
        help="output raw JSON",
    )

    p_call = sub.add_parser("call", help="Make a single LLM call")
    p_call.add_argument("prompt", nargs="+")
    p_call.add_argument("-p", "--provider", default="ollama")
    p_call.add_argument("-m", "--model",   required=True)
    p_call.add_argument("-s", "--system",  default="")
    p_call.add_argument("-t", "--timeout", type=int, default=60)
    p_call.add_argument(
        "--no-queue", action="store_true",
        help="bypass llmclient queue"
    )
    p_call.add_argument(
        "--json", action="store_true",
        help="output full LLMResult as JSON"
    )

    p_par = sub.add_parser(
        "parallel",
        help="Send N concurrent calls to test Ollama parallelism",
    )
    p_par.add_argument("prompt", nargs="+")
    p_par.add_argument("-p", "--provider", default="ollama")
    p_par.add_argument("-m", "--model",   required=True)
    p_par.add_argument("-s", "--system",  default="")
    p_par.add_argument("-n", "--n",       type=int, default=2)
    p_par.add_argument("-t", "--timeout", type=int, default=120)

    args = parser.parse_args()

    if args.dir:
        from pathlib import Path
        from llmclient import configure
        # --dir inspects a specific dir's own queue.db (legacy / isolated
        # queues); without it, the shared state queue is used.
        configure(data_dir=args.dir,
                  queue_file=Path(args.dir).expanduser() / "queue.db")

    if args.cmd == "status":
        from ._status import cmd_status
        cmd_status(args)
    elif args.cmd == "queue":
        from ._status import cmd_queue
        cmd_queue(args)
    elif args.cmd == "call":
        from ._call import cmd_call
        cmd_call(args)
    elif args.cmd == "parallel":
        from ._parallel import cmd_parallel
        cmd_parallel(args)
    elif args.cmd == "log":
        from ._log import cmd_log
        cmd_log(args)
    elif args.cmd == "reset":
        from ._status import cmd_reset
        cmd_reset(args)
    else:
        parser.print_help()
        sys.exit(1)
