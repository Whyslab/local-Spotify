"""Canonical production launcher for the local-Spotify adder service."""

import uvicorn

from .app import HOST, PORT


def main() -> None:
    uvicorn.run(
        "adder.app:app",
        host=HOST,
        port=PORT,
    )


if __name__ == "__main__":
    main()
