"""Console entry point for local stdio MCP transport."""

from .server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
