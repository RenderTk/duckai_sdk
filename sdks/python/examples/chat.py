"""Run: python examples/chat.py 'Hello' --stream --file document.pdf"""

import argparse
from pathlib import Path

from duckai import DuckAI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--file", type=Path, action="append", default=[])
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    with DuckAI() as ai:
        if args.stream:
            with ai.stream(args.prompt, files=args.file) as stream:
                for chunk in stream:
                    print(chunk, end="", flush=True)
            print()
        else:
            print(ai.chat(args.prompt, files=args.file).text)


if __name__ == "__main__":
    main()
