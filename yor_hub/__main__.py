"""python -m yor_hub --config config/pi.yaml"""
import argparse
from pathlib import Path

import uvicorn
import yaml

from .app import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="yor_hub")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "pi.yaml"))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None)
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    port = a.port or cfg["host"].get("port", 8080)
    uvicorn.run(create_app(a.config), host=a.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
