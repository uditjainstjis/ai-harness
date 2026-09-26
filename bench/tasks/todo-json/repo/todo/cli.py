import argparse
import sys

from .store import Store


def main(argv=None, store=None, out=None):
    out = out or sys.stdout
    store = store or Store()
    parser = argparse.ArgumentParser(prog="todo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("text")
    add.add_argument("--priority", type=int, default=2)
    sub.add_parser("list")
    done = sub.add_parser("done")
    done.add_argument("index", type=int)
    args = parser.parse_args(argv)

    items = store.load()
    if args.cmd == "add":
        items.append({"text": args.text, "priority": args.priority, "done": False})
        store.save(items)
        print(f"added #{len(items)}", file=out)
    elif args.cmd == "list":
        for i, item in enumerate(items, 1):
            mark = "x" if item["done"] else " "
            print(f"{i}. [{mark}] {item['text']} (p{item['priority']})", file=out)
    elif args.cmd == "done":
        items[args.index - 1]["done"] = True
        store.save(items)
        print(f"completed #{args.index}", file=out)
    return 0
