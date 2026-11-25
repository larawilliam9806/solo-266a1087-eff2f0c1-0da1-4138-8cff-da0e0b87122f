"""WSGI 入口。生产使用 waitress（纯 Python，便于离线镜像构建）。"""

from __future__ import annotations

import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        from waitress import serve
    except ImportError:  # 开发时缺 waitress 也可启动
        app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
    else:
        serve(app, host=host, port=port, threads=int(os.environ.get("THREADS", "8")))
