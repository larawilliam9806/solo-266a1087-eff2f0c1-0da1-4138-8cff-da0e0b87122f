"""页面与 JSON 接口。"""

from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, service
from .manifest import ManifestError

main_bp = Blueprint("main", __name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    best = request.accept_mimetypes.best
    return (
        request.is_json
        or best == "application/json"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _error_response(exc: service.HandoverError):
    if _wants_json():
        payload = {"error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return jsonify(payload), exc.status
    return render_template("error.html", code=exc.status, message=exc.message), exc.status


def _manifest_upload():
    upload = request.files.get("manifest")
    if upload is not None and upload.filename:
        data = upload.read(current_app.config["MAX_MANIFEST_BYTES"] + 1)
        if len(data) > current_app.config["MAX_MANIFEST_BYTES"]:
            raise service.HandoverError("清单超过 4 MiB，拒绝处理", 413)
        return data, upload.filename
    text = request.form.get("manifest_text")
    if text and text.strip():
        payload = text.encode("utf-8")
        if len(payload) > current_app.config["MAX_MANIFEST_BYTES"]:
            raise service.HandoverError("清单超过 4 MiB，拒绝处理", 413)
        return payload, "inline-manifest.json"
    raise service.HandoverError("请上传清单文件或粘贴清单内容", 400)


def _state_json(state: dict) -> dict:
    delivery = state["delivery"]
    manifest = state["manifest"]
    target = None
    if state["recovery_allowed"]:
        for shard in state["shards"]:
            if shard["index"] == state["missing"][0]:
                target = {
                    "index": shard["index"],
                    "size": shard["size"],
                    "sha256": shard["sha256"],
                }
                break
    return {
        "id": delivery["id"],
        "label": delivery["label"],
        "manifest_name": delivery["manifest_name"],
        "manifest_hash": delivery["manifest_hash"],
        "created_at": delivery["created_at"],
        "updated_at": delivery["updated_at"],
        "download_count": delivery["download_count"],
        "last_download_at": delivery["last_download_at"],
        "total_shards": state["total_count"],
        "total_bytes": manifest.total_bytes,
        "accepted_count": state["accepted_count"],
        "accepted_bytes": state["accepted_bytes"],
        "complete": state["complete"],
        "recovery_allowed": state["recovery_allowed"],
        "missing": state["missing"],
        "checkshard": {
            "length": manifest.max_size,
            "base_shard": manifest.longest_indices[0] if manifest.longest_indices else None,
        },
        "recovery_target": target,
        "recovered_index": delivery["recovered_index"],
        "used_recovery": delivery["recovered_index"] is not None,
        "download_ready": state["complete"],
        "download_url": url_for("api.download_package", delivery_id=delivery["id"])
        if state["complete"]
        else None,
        "shards": [
            {
                "index": s["index"],
                "size": s["size"],
                "sha256": s["sha256"],
                "accepted": s["accepted"],
                "source": s["source"],
                "accepted_at": s["accepted_at"],
            }
            for s in state["shards"]
        ],
    }


# ---------------------------------------------------------------- 页面


@main_bp.get("/healthz")
def healthz():
    try:
        conn = db.get_db()
        conn.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 - 健康检查只关心可用性
        return jsonify({"status": "degraded", "database": "unavailable"}), 503
    return jsonify({"status": "ok", "database": "ok"})


@main_bp.get("/")
def index():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.*,
                  (SELECT COUNT(*) FROM shards s
                    WHERE s.delivery_id = d.id AND s.accepted_at IS NULL) AS missing_count
           FROM deliveries d
           ORDER BY d.updated_at DESC, d.id DESC
           LIMIT 200"""
    ).fetchall()
    return render_template("index.html", deliveries=[dict(r) for r in rows])


@main_bp.post("/packages")
def create_package_page():
    try:
        manifest_bytes, filename = _manifest_upload()
        delivery_id, created = service.create_delivery(manifest_bytes, filename)
    except ManifestError as exc:
        if _wants_json():
            return jsonify({"error": exc.message, "details": exc.details}), 422
        for detail in (exc.details or [exc.message])[:10]:
            flash(detail, "error")
        return redirect(url_for("main.index"))
    except service.HandoverError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(exc.message, "error")
        return redirect(url_for("main.index"))

    if _wants_json():
        state = service.delivery_state(delivery_id)
        payload = _state_json(state)
        payload["created"] = created
        payload["reused"] = not created
        payload["status"] = "created" if created else "existing"
        return jsonify(payload), (201 if created else 200)
    flash(
        "交接包已建立" if created else "同一份清单已有交付记录，已沿用原记录",
        "ok",
    )
    return redirect(url_for("main.package_detail", delivery_id=delivery_id))


@main_bp.get("/packages/<int:delivery_id>")
def package_detail(delivery_id: int):
    try:
        state = service.delivery_state(delivery_id)
    except service.HandoverError as exc:
        return _error_response(exc)
    return render_template("package.html", state=state)


@main_bp.post("/packages/<int:delivery_id>/shards/<int:index>")
def upload_shard_page(delivery_id: int, index: int):
    upload = request.files.get("file") or request.files.get("shard")
    try:
        result = service.accept_shard(delivery_id, index, upload)
    except service.HandoverError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(exc.message, "error")
        return redirect(url_for("main.package_detail", delivery_id=delivery_id))
    if _wants_json():
        result["state"] = _state_json(service.delivery_state(delivery_id))
        return jsonify(result), (200 if result["reused"] else 201)
    flash(result["message"], "ok" if not result["reused"] else "info")
    return redirect(url_for("main.package_detail", delivery_id=delivery_id))


@main_bp.post("/packages/<int:delivery_id>/recover")
def recover_page(delivery_id: int):
    raw_index = request.form.get("index") or request.form.get("shard_index")
    upload = request.files.get("checkshard") or request.files.get("file")
    try:
        if raw_index is None or not str(raw_index).strip().isdigit():
            raise service.HandoverError("请指定要恢复的缺片号", 400)
        result = service.recover_missing(delivery_id, int(raw_index), upload)
    except service.HandoverError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(exc.message, "error")
        return redirect(url_for("main.package_detail", delivery_id=delivery_id))
    if _wants_json():
        result["state"] = _state_json(service.delivery_state(delivery_id))
        return jsonify(result), (200 if result["reused"] else 201)
    flash(result["message"], "ok" if not result["reused"] else "info")
    return redirect(url_for("main.package_detail", delivery_id=delivery_id))


def _download_response(delivery_id: int):
    try:
        _delivery, manifest, stream = service.open_download(delivery_id)
    except service.HandoverError as exc:
        return _error_response(exc)
    # HEAD 只返回头部：不输出字节流，也不计下载次数。
    body = () if request.method == "HEAD" else stream()
    response = Response(body, mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="flow-section-{delivery_id}.bin"'
    )
    response.headers["Content-Length"] = str(manifest.total_bytes)
    response.headers["X-Shard-Order"] = ",".join(map(str, manifest.indices))
    response.headers["Cache-Control"] = "no-store"
    return response


@main_bp.get("/packages/<int:delivery_id>/download")
def download_page(delivery_id: int):
    return _download_response(delivery_id)


# ---------------------------------------------------------------- JSON API


@api_bp.post("/packages")
def create_package_api():
    try:
        manifest_bytes, filename = _manifest_upload()
        delivery_id, created = service.create_delivery(manifest_bytes, filename)
    except ManifestError as exc:
        return jsonify({"error": exc.message, "details": exc.details}), 422
    except service.HandoverError as exc:
        return _error_response(exc)
    payload = _state_json(service.delivery_state(delivery_id))
    payload["created"] = created
    payload["reused"] = not created
    return jsonify(payload), (201 if created else 200)


@api_bp.get("/packages")
def list_packages_api():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.*,
                  (SELECT COUNT(*) FROM shards s
                    WHERE s.delivery_id = d.id AND s.accepted_at IS NULL) AS missing_count
           FROM deliveries d ORDER BY d.id"""
    ).fetchall()
    return jsonify(
        {
            "packages": [
                {
                    "id": r["id"],
                    "label": r["label"],
                    "manifest_hash": r["manifest_hash"],
                    "total_shards": r["total_shards"],
                    "accepted_shards": r["total_shards"] - r["missing_count"],
                    "missing_count": r["missing_count"],
                    "complete": r["missing_count"] == 0,
                    "recovered_index": r["recovered_index"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]
        }
    )


@api_bp.get("/packages/<int:delivery_id>")
def package_detail_api(delivery_id: int):
    try:
        return jsonify(_state_json(service.delivery_state(delivery_id)))
    except service.HandoverError as exc:
        return _error_response(exc)


@api_bp.put("/packages/<int:delivery_id>/shards/<int:index>")
@api_bp.post("/packages/<int:delivery_id>/shards/<int:index>")
def upload_shard_api(delivery_id: int, index: int):
    upload = request.files.get("file") or request.files.get("shard")
    try:
        result = service.accept_shard(delivery_id, index, upload)
    except service.HandoverError as exc:
        return _error_response(exc)
    result["state"] = _state_json(service.delivery_state(delivery_id))
    return jsonify(result), (200 if result["reused"] else 201)


@api_bp.post("/packages/<int:delivery_id>/recover")
def recover_api(delivery_id: int):
    raw_index = request.form.get("index") or request.form.get("shard_index")
    if raw_index is None:
        payload = request.get_json(silent=True) or {}
        raw_index = payload.get("index")
    upload = request.files.get("checkshard") or request.files.get("file")
    try:
        if raw_index is None or not str(raw_index).strip().isdigit():
            raise service.HandoverError("请指定要恢复的缺片号", 400)
        result = service.recover_missing(delivery_id, int(raw_index), upload)
    except service.HandoverError as exc:
        return _error_response(exc)
    result["state"] = _state_json(service.delivery_state(delivery_id))
    return jsonify(result), (200 if result["reused"] else 201)


@api_bp.get("/packages/<int:delivery_id>/download")
def download_package(delivery_id: int):
    return _download_response(delivery_id)


@api_bp.get("/packages/<int:delivery_id>/manifest")
def manifest_api(delivery_id: int):
    try:
        state = service.delivery_state(delivery_id)
    except service.HandoverError as exc:
        return _error_response(exc)
    return Response(
        state["delivery"]["manifest_json"],
        mimetype="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="manifest-{delivery_id}.json"'
            )
        },
    )
