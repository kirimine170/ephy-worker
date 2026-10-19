"""Windowed PyInstaller entry point for the worker console."""

from ephy_worker.desktop.app import main


if __name__ == "__main__":
    raise SystemExit(main())
