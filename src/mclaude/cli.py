"""Command-line entry point for MClaude."""

import argparse
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    """Parse command-line options and run the application."""
    parser = argparse.ArgumentParser(
        prog="mclaude",
        description="MClaude: a Python coding agent built incrementally.",
        epilog="Project initialized. Model integration is the next step.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('mclaude')}"
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0
