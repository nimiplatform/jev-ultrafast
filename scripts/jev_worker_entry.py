"""PyInstaller entry of the packaged worker (jev-worker.exe). The same executable also starts the Browser
Harness daemon: worker.main() handles `-m browser_harness.daemon` when frozen."""

from jev_ultrafast.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
