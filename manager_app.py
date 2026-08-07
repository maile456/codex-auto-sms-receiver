from __future__ import annotations

import app as upstream_entry
from manager.blueprint import create_manager_blueprint
from src.settings import Settings
from src.webapp import create_app as create_upstream_app


def create_managed_app(settings: Settings, **upstream_kwargs):
    flask_app = create_upstream_app(settings, **upstream_kwargs)
    flask_app.register_blueprint(create_manager_blueprint(settings))
    return flask_app


def main() -> None:
    upstream_entry.create_app = create_managed_app
    upstream_entry.main()


if __name__ == "__main__":
    main()
