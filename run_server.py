from __future__ import annotations

import argparse

import uvicorn

from balance_chat.bootstrap import build_application


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    uvicorn.run(build_application(args.config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
