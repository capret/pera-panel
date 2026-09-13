import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys

from werkzeug.security import generate_password_hash

from .storage import write_json
from .save_import import UPLOAD_REQUEST_LIMIT
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="Pera Panel service and credential setup")
    parser.add_argument("command", choices=["serve", "init-admin"])
    parser.add_argument("--config", default=os.environ.get("PERA_CONFIG", "/etc/pera-panel/config.json"))
    parser.add_argument("--data", default="/var/lib/pera-panel")
    parser.add_argument("--game", default="/opt/pera-panel/game")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--prompt-password", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("The panel port must be between 1024 and 65535.")
    os.umask(0o077)
    path = Path(args.config).resolve()
    if args.command == "init-admin":
        if path.exists() and not args.reset:
            print("Existing credentials and settings preserved.")
            return
        config = json.loads(path.read_text()) if path.exists() else {
            "secret_key": secrets.token_urlsafe(48), "data_dir": str(Path(args.data).resolve()),
            "game_dir": str(Path(args.game).resolve()), "host": args.host, "port": args.port,
            "secure_cookie": False,
        }
        password = getpass.getpass("New password (12+ characters): ") if args.prompt_password else secrets.token_urlsafe(18)
        if len(password) < 12:
            parser.error("Use at least 12 characters.")
        config.update(username="admin", password_hash=generate_password_hash(password))
        write_json(path, config)
        print("Username: admin")
        if not args.prompt_password:
            print(f"Password: {password}")
        print(f"Configuration: {path}")
        return
    if not path.is_file():
        parser.error("Run init-admin first (see README.md).")
    config = json.loads(path.read_text(encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # A second worker must never spawn a second copy of the same game world.
    lock_file = None
    if sys.platform == "linux":
        import fcntl
        lock_path = Path(config["data_dir"]) / "panel.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another Pera Panel process already owns this data directory.")
    app = create_app(config)
    service = app.extensions["pera"]
    from waitress import create_server
    server = create_server(app, host=config.get("host", "127.0.0.1"), port=config.get("port", 8080),
                           threads=8, max_request_body_size=UPLOAD_REQUEST_LIMIT, clear_untrusted_proxy_headers=True)

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        service.autostart()
        logging.info("Pera Panel listening on %s:%s", config.get("host"), config.get("port"))
        server.run()
    finally:
        server.close()
        service.close()
        if lock_file:
            lock_file.close()


if __name__ == "__main__":
    main()
