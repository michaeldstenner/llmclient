import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="llmc",
        description="llmclient CLI — probe and diagnose LLM backends",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("status", help="Ollama state, connections, queue")
    sub.add_parser("queue",  help="llmclient queue state only")

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
    else:
        parser.print_help()
        sys.exit(1)
