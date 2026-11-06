from pathlib import Path
import secrets

from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy

from config import Config


db = SQLAlchemy()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.setdefault("CODEX_BRIDGE_CSRF_TOKEN", secrets.token_urlsafe(32))
    app.config.setdefault("LEARNING_PAGE_ROOT", str(Path(app.root_path).parent / "work"))
    db.init_app(app)

    from gamelearn.services.codex_bridge import CodexChatDispatcher, CodexExplanationDispatcher

    app.extensions["codex_explanations"] = CodexExplanationDispatcher(Path(app.root_path).parent)
    app.extensions["codex_chat"] = CodexChatDispatcher(Path(app.root_path).parent)

    from gamelearn.routes import main

    app.register_blueprint(main)

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", heading="Page not found", message="GameLearn could not find that page."), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template(
            "error.html",
            heading="Something went wrong",
            message="GameLearn could not complete that request. Your Unity project was not changed.",
        ), 500

    with app.app_context():
        db.create_all()
        # create_all() does not add columns to tables in an existing SQLite
        # database. Keep local pre-release databases usable as fields are added.
        from sqlalchemy import inspect, text

        columns = {
            column["name"] for column in inspect(db.engine).get_columns("session")
        }
        missing_session_columns = {
            "name": "VARCHAR(200)",
            "prompt": "TEXT",
            "notes": "TEXT",
        }
        project_columns = {
            column["name"] for column in inspect(db.engine).get_columns("project")
        }
        with db.engine.begin() as connection:
            for column, sql_type in missing_session_columns.items():
                if column not in columns:
                    connection.execute(
                        text(f'ALTER TABLE session ADD COLUMN "{column}" {sql_type}')
                    )
            if "is_archive" not in project_columns:
                connection.execute(
                    text("ALTER TABLE project ADD COLUMN is_archive BOOLEAN NOT NULL DEFAULT 0")
                )

    if not app.config.get("TESTING") and app.config.get("START_SESSION_MONITOR", True):
        from gamelearn.services.session_service import SessionMonitor

        monitor = SessionMonitor(app)
        monitor.start()
        app.extensions["gamelearn_monitor"] = monitor

    return app
