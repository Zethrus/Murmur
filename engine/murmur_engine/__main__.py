"""Entry point for both python -m murmur_engine and the frozen sidecar."""
import argparse
import logging
import os
from pathlib import Path
import sys


def main() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
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
