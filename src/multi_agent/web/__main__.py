"""Launch the local workspace HTTP service."""

import argparse
from pathlib import Path

from .server import WorkspaceServer
from .workspace import Workspace


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the local workspace API.")
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--claude-bin", default="claude")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        workspace = Workspace.open(args.repository, args.data_dir)
        with WorkspaceServer(
            args.port, workspace, {"codex": args.codex_bin, "claude": args.claude_bin}
        ) as service:
            print(service.origin, flush=True)
            try:
                service.serve_forever()
            except KeyboardInterrupt:
                pass
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
