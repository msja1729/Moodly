"""Entry point of the packaged app (AmbientMood.exe).

    AmbientMood.exe              the tray app, which starts the daemon itself
    AmbientMood.exe --daemon ... the background daemon (started by the tray app)

One executable plays both roles so the installer ships a single program.
In development run the modules directly instead (see README.md).
"""
import sys


def main() -> int:
    if "--daemon" in sys.argv[1:]:
        from backend.daemon import main as daemon_main

        daemon_main([a for a in sys.argv[1:] if a != "--daemon"])
        return 0
    from frontend.app import main as app_main

    return app_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
