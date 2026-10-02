"""ColabConnect: create, configure and run notebooks on Google Colab, with an MCP server."""

__version__ = "0.1.0"


def main() -> None:
    from .main import main as _main

    _main()
