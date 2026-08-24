"""Canonical production launcher for the local-Spotify adder service."""

import uvicorn

# Import from config, not via app: app does not use HOST/PORT itself, so a
# linter's unused-import pass will happily delete them from there.
from .config import HOST, PORT


def main() -> None:
    uvicorn.run(
        "adder.app:app",
        host=HOST,
        port=PORT,
    )


if __name__ == "__main__":
    main()
