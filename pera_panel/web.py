from collections import OrderedDict, deque
from datetime import timedelta
import hmac
import json
import secrets
import shutil
import time
from pathlib import Path
import tempfile

from flask import Flask, abort, jsonify, render_template, request, session
import psutil
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash

from . import __version__
from .service import Service
from .save_import import CHUNK, MAX_UPLOAD_BYTES, UPLOAD_REQUEST_LIMIT
from .storage import PanelError, line
from .workshop import Workshop


def create_app(config, service=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=config["secret_key"], MAX_CONTENT_LENGTH=256 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=config.get("secure_cookie", False),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    service = service or Service(config["data_dir"], config["game_dir"])
    app.extensions["pera"] = service
    workshop = app.extensions["workshop"] = Workshop()
    translations = json.loads((Path(__file__).parent / "static" / "zh-CN.json").read_text(encoding="utf-8"))
    attempts = OrderedDict()
    # A password reset invalidates existing sessions even when the signing key is preserved.
    auth_version = config["password_hash"][-32:]

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def authenticated():
        return session.get("auth") == auth_version

    @app.before_request
    def protect():
        if request.endpoint == "import_save":
            request.max_content_length = UPLOAD_REQUEST_LIMIT
        if request.path.startswith("/api/"):
            public = request.path in ("/api/session", "/api/login")
            if not public and not authenticated():
                raise PanelError("Sign in to continue.", 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                supplied = request.headers.get("X-CSRF-Token", "")
                if not session.get("csrf") or not hmac.compare_digest(supplied, session["csrf"]):
                    raise PanelError("Your session expired. Reload the page and try again.", 403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(PanelError)
    def panel_error(error):
        return jsonify(error=str(error), error_i18n=error.i18n), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise PanelError("Expected a JSON object.")
        return data

    def operation(title, callback, identifier=None):
        return jsonify(job=service.submit(title, callback, world_id=identifier)), 202

    @app.get("/")
    @app.get("/overview")
    @app.get("/archives")
    @app.get("/worlds/<identifier>/<tab>")
    def index(identifier=None, tab=None):
        if identifier is not None:
            if tab not in ("overview", "settings", "mods", "backups", "logs"):
                abort(404)
            service.store.world_path(identifier)
        return render_template("index.html" if authenticated() else "login.html",
                               csrf=csrf_token(), version=__version__, translations=translations)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/api/session")
    def get_session():
        return jsonify(authenticated=authenticated(), csrf=csrf_token())

    @app.post("/api/login")
    def login():
        data = body()
        address, now = request.remote_addr or "local", time.monotonic()
        with service.lock:
            history = attempts.setdefault(address, deque(maxlen=10))
            while history and history[0] < now - 300:
                history.popleft()
            if len(history) >= 10:
                raise PanelError("Too many login attempts. Try again in five minutes.", 429)
            history.append(now)
            attempts.move_to_end(address)
            if len(attempts) > 1024:
                attempts.popitem(last=False)
        username, password = data.get("username", ""), data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
            raise PanelError("Invalid username or password.", 401)
        valid = check_password_hash(config["password_hash"], password)
        if not valid or not hmac.compare_digest(username.encode(), config["username"].encode()):
            raise PanelError("Invalid username or password.", 401)
        with service.lock:
            attempts.pop(address, None)
        session.clear()
        session.permanent = True
        session["auth"] = auth_version
        return jsonify(csrf=csrf_token())

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        memory = psutil.virtual_memory()
        disk = shutil.disk_usage(service.store.root)
        worlds = [{**service.store.public(world), "runtime": service.runtime.status(world["id"])}
                  for world in service.store.all()]
        return jsonify(version=__version__, worlds=worlds, jobs=service.job_list(), busy=service.busy,
                       host={"cpu_percent": psutil.cpu_percent(), "memory_percent": memory.percent,
                             "memory_used_gb": round(memory.used / 1024**3, 1),
                             "memory_total_gb": round(memory.total / 1024**3, 1),
                             "disk_free_gb": round(disk.free / 1024**3, 1)},
                       game_installed=(service.store.game / "bin64" /
                                       "dontstarve_dedicated_server_nullrenderer_x64").is_file())

    @app.post("/api/workshop/details")
    def workshop_details():
        return jsonify(mods=workshop.details(body().get("ids")))

    @app.post("/api/worlds")
    def create_world():
        data = body()
        return operation("Create world", lambda: service.create(data))

    @app.get("/api/archived-worlds")
    def archived_worlds():
        with service.lock:
            if service.busy:
                raise PanelError("Another operation is in progress. Wait for it to finish.", 409)
            return jsonify(worlds=service.archives.list())

    @app.delete("/api/archived-worlds/<identifier>")
    def delete_archived_world(identifier):
        data = body()
        return operation("Delete archived world", lambda: service.delete_archived(identifier, data.get("confirmation")), identifier)

    @app.patch("/api/worlds/<identifier>")
    def configure_world(identifier):
        data = body()
        return operation("Save world settings", lambda: service.configure(identifier, data), identifier)

    @app.delete("/api/worlds/<identifier>")
    def delete_world(identifier):
        data = body()
        return operation("Archive world", lambda: service.delete(identifier, data.get("confirmation")), identifier)

    @app.post("/api/worlds/<identifier>/actions/<action>")
    def world_action(identifier, action):
        data = body()
        service.store.get(identifier)
        if action in ("start", "stop", "restart"):
            return operation(f"{action.title()} world", lambda: getattr(service, action)(identifier), identifier)
        if action in ("save", "announce"):
            message = line(data.get("message", ""), "Announcement", 300, required=action == "announce")
            return operation(f"{action.title()} command", lambda: service.runtime.command(identifier, action, message), identifier)
        if action == "backup":
            return operation("Create backup", lambda: service.backup(identifier, data.get("label", "Manual backup")), identifier)
        if action == "rollback":
            return operation("Native rollback", lambda: service.rollback(identifier, data.get("count"), data.get("confirmation")), identifier)
        if action == "update-mods":
            def update_mods():
                service.require_stopped(identifier)
                if service.runtime.active():
                    raise PanelError("Stop the active world before downloading mods.", 409)
                service.runtime.update_mods(service.store.get(identifier))
            return operation("Download Workshop mods", update_mods, identifier)
        raise PanelError("Unknown action.", 404)

    @app.get("/api/worlds/<identifier>/players")
    def players(identifier):
        service.store.get(identifier)
        if not service.busy:
            service.runtime.request_players(identifier)
        return jsonify(players=service.players.list(identifier))

    @app.get("/api/worlds/<identifier>/characters")
    def saved_characters(identifier):
        with service.lock:
            if service.busy:
                raise PanelError("Another operation is in progress. Wait for it to finish.", 409)
            return jsonify(characters=service.character_list(identifier))

    @app.post("/api/worlds/<identifier>/recover-character")
    def recover_character(identifier):
        data = body()
        service.store.get(identifier)
        return operation("Recover character", lambda: service.recover_character(
            identifier, data.get("source"), data.get("userid"), data.get("confirmation"), data.get("destination")), identifier)

    @app.get("/api/worlds/<identifier>/backups")
    def backups(identifier):
        return jsonify(backups=service.backups(identifier))

    @app.post("/api/worlds/<identifier>/backups/<snapshot>/restore")
    def restore(identifier, snapshot):
        data = body()
        if data.get("confirmation") != service.store.get(identifier)["name"]:
            raise PanelError("Type the world name exactly to roll back.")
        return operation("Restore backup", lambda: service.restore(identifier, snapshot), identifier)

    @app.delete("/api/worlds/<identifier>/backups/<snapshot>")
    def delete_backup(identifier, snapshot):
        return operation("Delete backup", lambda: service.delete_backup(identifier, snapshot), identifier)

    @app.post("/api/worlds/<identifier>/import-save")
    def import_save(identifier):
        current = service.store.get(identifier)
        if service.busy or service.closing or not service.upload_lock.acquire(blocking=False):
            raise PanelError("Another operation or upload is in progress. Wait for it to finish.", 409)
        temporary = None
        try:
            confirmation = request.form.get("confirmation", "")
            inherit_mods = request.form.get("inherit_mods", "true")
            if inherit_mods not in ("true", "false"):
                raise PanelError("inherit_mods must be true or false.")
            if confirmation != current["name"]:
                raise PanelError("Type the world name exactly to replace its save.")
            files = request.files.getlist("save_zip")
            if len(files) != 1 or not files[0].filename or not files[0].filename.lower().endswith(".zip"):
                raise PanelError("Choose one local save ZIP file.")
            temporary = tempfile.TemporaryDirectory(prefix=".upload-", dir=service.store.root)
            uploaded = Path(temporary.name) / "save.zip"
            size = 0
            with uploaded.open("xb") as output:
                while chunk := files[0].stream.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise PanelError("The save ZIP must be 256 MiB or smaller.", 413)
                    output.write(chunk)
            if not size:
                raise PanelError("The uploaded ZIP is empty.")
            def replace_save(directory=temporary):
                try:
                    return service.import_save(identifier, uploaded, confirmation, inherit_mods == "true")
                finally:
                    directory.cleanup()
            result = operation("Import local save", replace_save, identifier)
            temporary = None  # The operation owns cleanup after request file streams close.
            return result
        finally:
            if temporary:
                temporary.cleanup()
            service.upload_lock.release()

    @app.get("/api/worlds/<identifier>/logs/<shard>")
    def logs(identifier, shard):
        service.store.get(identifier)
        return jsonify(log=service.runtime.log(identifier, shard))

    return app
