import sys


def check_deps():
    """Check if required dependencies are installed."""
    missing = []

    try:
        import anyio
    except ImportError:
        missing.append("anyio")

    try:
        import typer
    except ImportError:
        missing.append("typer")

    if missing:
        deps_str = ", ".join(missing)
        message = f"""Missing Python dependencies: {deps_str}
Install with:
  python -m pip install anyio typer
"""
        sys.stderr.write(message)
        sys.exit(1)

    try:
        import rich
    except ImportError:
        sys.stderr.write(
            "Note: 'rich' is not installed. Logging will use plain text.\n"
            "Install for colored output: python -m pip install rich\n"
        )
