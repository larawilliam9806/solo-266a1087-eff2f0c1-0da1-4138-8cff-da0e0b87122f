"""流速断面交接台 Flask 应用工厂。"""

from __future__ import annotations

import os
import secrets

from flask import Flask, jsonify, render_template, request

from . import db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_secret_key() -> str:
    return os.environ.get("SECRET_KEY") or secrets.token_hex(32)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
    )

    data_dir = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
    config = {
        "DATA_DIR": data_dir,
        "SECRET_KEY": _default_secret_key(),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        # 默认不限请求体大小（大断面单片可能远超 8 GiB）；需要时可设 MAX_UPLOAD_BYTES。
        "MAX_CONTENT_LENGTH": int(os.environ["MAX_UPLOAD_BYTES"])
        if os.environ.get("MAX_UPLOAD_BYTES")
        else None,
        "MAX_FORM_MEMORY_SIZE": 4 * 1024 * 1024,
        # 恢复/下载/上传的固定分块大小：内存开销与片大小无关。
        "CHUNK_SIZE": int(os.environ.get("CHUNK_SIZE", str(1024 * 1024))),
        "MAX_MANIFEST_BYTES": 4 * 1024 * 1024,
    }
    if test_config:
        config.update(test_config)

    config["DATA_DIR"] = os.path.abspath(config["DATA_DIR"])
    config.setdefault("DATABASE", os.path.join(config["DATA_DIR"], "deliveries.sqlite3"))
    config["DATABASE"] = os.path.abspath(config["DATABASE"])
    app.config.update(config)

    for path in (
        app.config["DATA_DIR"],
        os.path.join(app.config["DATA_DIR"], "packages"),
        os.path.join(app.config["DATA_DIR"], "tmp"),
        os.path.dirname(app.config["DATABASE"]),
    ):
        if path:
            os.makedirs(path, exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.init_db(app.config["DATABASE"])

    from .views import api_bp, main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)

    @app.template_filter("filesize")
    def filesize(value) -> str:
        try:
            size = int(value)
        except (TypeError, ValueError):
            return "—"
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        number = float(size)
        for unit in units:
            if number < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{size} B"
                return f"{number:.2f} {unit}"
            number /= 1024
        return f"{size} B"

    @app.template_filter("ts")
    def ts(value) -> str:
        if not value:
            return "—"
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1]
        return text.replace("T", " ") + " UTC"

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "未找到"}), 404
        return render_template("error.html", code=404, message="页面或交付包不存在。"), 404

    @app.errorhandler(413)
    def too_large(_error):
        limit = app.config["MAX_CONTENT_LENGTH"]
        detail = (
            f"上传内容超过请求上限（{limit} 字节），可调整 MAX_UPLOAD_BYTES 环境变量。"
            if limit
            else "表单字段过大（清单上限 4 MiB）。"
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": detail}), 413
        return render_template("error.html", code=413, message=detail), 413

    return app
