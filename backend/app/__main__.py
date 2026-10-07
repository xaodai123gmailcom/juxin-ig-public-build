from __future__ import annotations

import argparse
import os

# Bound only Uvicorn's HTTP connection/request drain. A reset Windows Proactor
# transport can miss connection_lost and remain in Uvicorn's connection set.
# Application lifespan cleanup must still run to completion: it owns durable
# writes, browser leases and the database instance lock.
HTTP_DRAIN_TIMEOUT_SECONDS = 5.0


def create_server(app, *, host: str, port: int):
    import uvicorn

    return uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, access_log=False,
        timeout_graceful_shutdown=HTTP_DRAIN_TIMEOUT_SECONDS,
    ))


try:
    from .parent_watchdog import start_parent_watchdog_from_env
except ImportError:  # PyInstaller analyzes this file as a top-level script.
    from app.parent_watchdog import start_parent_watchdog_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the IG Audience Collector local core")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address only")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("IGAC_PORT", os.environ.get("COLLECTOR_CORE_PORT", "8765"))),
    )
    parser.add_argument("--verify-collection-completion", action="store_true",
                        help="Run isolated offline collection-completion verification and exit")
    parser.add_argument("--verify-standalone-nurture", action="store_true",
                        help="Run isolated offline standalone nurture verification and exit")
    parser.add_argument("--verify-nurture-cleanup-upgrade", metavar="MANIFEST",
                        help="Verify externally persisted offline R6.2 orphan-hold upgrade and exit")
    args = parser.parse_args()
    if args.verify_nurture_cleanup_upgrade:
        try:
            from .nurture_cleanup_upgrade_selftest import main as verify_nurture_cleanup_upgrade
        except ImportError:
            from app.nurture_cleanup_upgrade_selftest import main as verify_nurture_cleanup_upgrade
        verify_nurture_cleanup_upgrade(args.verify_nurture_cleanup_upgrade)
        return
    if args.verify_standalone_nurture:
        try:
            from .standalone_nurture_selftest import main as verify_standalone_nurture
        except ImportError:  # PyInstaller top-level entry point.
            from app.standalone_nurture_selftest import main as verify_standalone_nurture
        verify_standalone_nurture()
        return
    if args.verify_collection_completion:
        try:
            from .collection_completion_selftest import main as verify_collection_completion
        except ImportError:  # PyInstaller top-level entry point.
            from app.collection_completion_selftest import main as verify_collection_completion
        verify_collection_completion()
        return

    # Electron owns this local-only service.  If the desktop process crashes or is
    # force-killed, do not leave an orphan Core occupying the configured port and
    # polling BitBrowser indefinitely.  Direct CLI runs omit IGAC_PARENT_PID and
    # therefore retain their normal independent lifetime.
    parent_watchdog = start_parent_watchdog_from_env()

    # Import after argument parsing so storage/domain tests do not require FastAPI/Uvicorn.
    try:
        from .config import Settings
        from .main import create_app
    except ImportError:  # PyInstaller analyzes this file as a top-level script.
        from app.config import Settings
        from app.main import create_app

    settings = Settings.from_env()
    settings = Settings(
        startup_token=settings.startup_token,
        database_path=settings.database_path,
        data_dir=settings.data_dir,
        bitbrowser_url=settings.bitbrowser_url,
        bitbrowser_api_key=settings.bitbrowser_api_key,
        session_hours=settings.session_hours,
        bind_host=args.host,
        bind_port=args.port,
    )
    settings.validate_bind()
    app = create_app(settings)
    server = create_server(app, host=settings.bind_host, port=settings.bind_port)
    def request_shutdown():
        server.should_exit = True
    app.state.request_shutdown = request_shutdown
    try:
        server.run()
    finally:
        if parent_watchdog is not None:
            parent_watchdog.stop()


if __name__ == "__main__":
    main()
