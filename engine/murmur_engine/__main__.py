"""Entry point for both python -m murmur_engine and the frozen engine."""
import argparse
import logging
from pathlib import Path
import sys


def main() -> None:
    from murmur_engine import runtime
    runtime.prepare()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Murmur offline speech engine")
    parser.add_argument("--models-dir", required=True, type=Path)
    args = parser.parse_args()
    from murmur_engine.models import Models
    from murmur_engine.stt import Speech
    from murmur_engine.rpc import Server
    models = Models(args.models_dir)
    Server(Speech(models), models, sys.stdout).run(sys.stdin)


if __name__ == "__main__":
    main()
