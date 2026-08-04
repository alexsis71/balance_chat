from __future__ import annotations

import argparse
import importlib
import sys
from http.server import ThreadingHTTPServer

from balance_chat.compat.pipeline_runtime import RuntimeConfig
from balance_chat.hosting import create_gateway_handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--v2-upstream", default="http://127.0.0.1:8790")
    args = parser.parse_args()

    runtime = RuntimeConfig.from_json(args.config)
    pipeline_root = str(runtime.pipeline_root)
    if pipeline_root not in sys.path:
        sys.path.insert(0, pipeline_root)
    legacy = importlib.import_module("app_server")
    handler = create_gateway_handler(legacy.AppHandler, args.v2_upstream)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"AI Balances legacy UI: http://{args.host}:{args.port}/")
    print(f"AI Balances Context Chat V2: http://{args.host}:{args.port}/v2/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        legacy.close_runtime()
        server.server_close()


if __name__ == "__main__":
    main()
