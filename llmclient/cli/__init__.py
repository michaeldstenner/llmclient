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
    parser.add_argument(
        "--config-dir", metavar="PATH",
        help="extra config dir whose config.yaml overlays "
             "~/.config/llmclient/config.yaml",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("status", help="Ollama state, connections, queue")
    sub.add_parser("queue",  help="llmclient queue state only")
    sub.add_parser("reset",  help="Reset all tripped circuit breakers")

    p_prov = sub.add_parser(
        "providers",
        help="Configured providers: url + whether a key resolves",
    )
    p_prov.add_argument("--json", action="store_true", help="output raw JSON")

    p_models = sub.add_parser(
        "models",
        help="Named models from config.yaml + each provider's catalog",
    )
    p_models.add_argument(
        "-p", "--provider", metavar="NAME",
        help="list one provider's full catalog (default: summary of all)",
    )
    p_models.add_argument(
        "--filter", metavar="TEXT",
        help="only models whose id contains TEXT (case-insensitive)",
    )
    p_models.add_argument(
        "--limit", type=int, default=40, metavar="N",
        help="max models to print per provider (default: 40)",
    )
    p_models.add_argument(
        "--all", action="store_true",
        help="print every model, ignoring --limit",
    )
    p_models.add_argument(
        "--no-probe", action="store_true",
        help="skip network calls; show configuration only",
    )
    p_models.add_argument(
        "--timeout", type=int, default=15, metavar="S",
        help="per-provider HTTP timeout (default: 15)",
    )
    p_models.add_argument("--json", action="store_true", help="output raw JSON")

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
    p_call.add_argument(
        "-p", "--provider", default=None,
        help="provider (default: ollama, or the named model's provider)",
    )
    p_call.add_argument(
        "-m", "--model", required=True,
        help="model id, or a name from config.yaml's `models:` section "
             "(see `llmc models`)",
    )
    p_call.add_argument("-s", "--system",  default="")
    p_call.add_argument("-t", "--timeout", type=int, default=None)
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

    if args.dir or args.config_dir:
        from pathlib import Path
        from llmclient import configure
        # --dir inspects a specific dir's own queue.db (legacy / isolated
        # queues); without it, the shared state queue is used.  configure()
        # sets every knob at once, so both flags go through one call.
        kwargs: dict = {}
        if args.dir:
            kwargs["data_dir"]   = args.dir
            kwargs["queue_file"] = Path(args.dir).expanduser() / "queue.db"
        if args.config_dir:
            kwargs["config_dir"] = args.config_dir
        configure(**kwargs)

    if args.cmd == "status":
        from ._status import cmd_status
        cmd_status(args)
    elif args.cmd == "queue":
        from ._status import cmd_queue
        cmd_queue(args)
    elif args.cmd == "providers":
        from ._discover import cmd_providers
        cmd_providers(args)
    elif args.cmd == "models":
        from ._discover import cmd_models
        cmd_models(args)
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
