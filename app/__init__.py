import os
from flask import Flask


def create_app():
    app = Flask(__name__)
    app.secret_key = os.urandom(24)

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app.config["SITES_DIR"] = os.path.join(base_dir, "cloned_sites")
    app.config["CERTS_DIR"] = os.path.join(base_dir, ".certs")
    app.config["STATE_FILE"] = os.path.join(base_dir, ".session_state.json")
    app.config["PID_FILE"] = os.path.join(base_dir, ".server_pid")

    os.makedirs(app.config["SITES_DIR"], exist_ok=True)
    os.makedirs(app.config["CERTS_DIR"], exist_ok=True)

    from .services.hijack import SessionManager

    app.config["SESSION_MANAGER"] = SessionManager(
        app.config["STATE_FILE"], app.config["PID_FILE"]
    )

    from .routes import bp

    app.register_blueprint(bp)

    return app
