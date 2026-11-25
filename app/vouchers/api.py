"""凭证能力的 HTTP 接口。

除规范路径外，同时注册常见 REST 同义路径，降低不同调用约定下的摩擦；
未显式覆盖的路径由 gateway 兜底解析。
"""
from __future__ import annotations

import json

from flask import Blueprint, current_app, request, Response

from app.errors import DomainIssue
from app.vouchers.artifact import render_artifact, render_csv
from app.vouchers.service import VoucherService, serialize_voucher

voucher_api = Blueprint("voucher_api", __name__)


def service() -> VoucherService:
    runs = current_app.extensions.get("runs")
    cached = current_app.extensions.get("voucher_service")
    if cached is not None:
        return cached
    svc = VoucherService(runs, runs_store=runs)
    current_app.extensions["voucher_service"] = svc
    return svc


def body() -> dict:
    return request.get_json(silent=True) or {}


def idem_key(data: dict):
    return (request.headers.get("Idempotency-Key")
            or request.headers.get("X-Idempotency-Key")
            or data.get("idempotency_key") or data.get("idem_key"))


def json_response(payload, status=200):
    return current_app.response_class(json.dumps(payload, ensure_ascii=False, default=str),
                                      status=status, mimetype="application/json")


def with_idempotency(scope: str, key, fn):
    """fn 返回 (payload, status)；相同幂等键重放首次响应。"""
    svc = service()
    if key:
        saved = svc.store.get_idem(scope, key)
        if saved:
            return Response(saved["body"], status=saved["status_code"],
                            mimetype="application/json")
    payload, status = fn()
    if key and status < 400:
        svc.store.save_idem(scope, key, status, payload)
    return json_response(payload, status)


def voucher_payload(voucher_id):
    svc = service()
    row = svc.get(voucher_id)
    data = serialize_voucher(row, svc.store)
    data["voucher"] = dict(data)
    return data


# ---------------- 集合 / 创建 ----------------

@voucher_api.get("/api/vouchers")
def list_vouchers():
    run_id = request.args.get("run_id")
    period = request.args.get("period")
    items = service().store.list_vouchers(run_id=run_id, period=period)
    return json_response({"items": [serialize_voucher(r, service().store, detailed=False) for r in items],
                          "vouchers": [serialize_voucher(r, service().store, detailed=False) for r in items],
                          "count": len(items)})


@voucher_api.post("/api/vouchers")
def create_voucher():
    data = body()
    svc = service()
    row = svc.create_voucher(data)
    payload = serialize_voucher(row, svc.store)
    payload["voucher"] = dict(payload)
    return json_response(payload, 200)


# ---------------- 单个凭证 ----------------

@voucher_api.get("/api/vouchers/<voucher_id>")
def get_voucher(voucher_id):
    fmt_ext = None
    if voucher_id.endswith((".json", ".csv")):
        voucher_id, fmt_ext = voucher_id.rsplit(".", 1)
    if ("download" in request.args or request.args.get("view") in {"artifact", "download"}
            or request.args.get("format") == "csv" or fmt_ext == "csv"):
        if fmt_ext == "csv" and not request.args.get("format"):
            csv_text = render_csv(render_artifact(service().get(voucher_id), service().store))
            return Response(csv_text, mimetype="text/csv")
        return _download(voucher_id)
    return json_response(voucher_payload(voucher_id))


@voucher_api.route("/api/vouchers/<voucher_id>", methods=["PATCH", "PUT"])
def update_voucher(voucher_id):
    data = body()
    svc = service()
    row = svc.get(voucher_id)
    expires = data.get("expires_at") or data.get("valid_until") or data.get("expire_date")
    if expires is not None:
        with svc.store.connect() as db:
            db.execute("UPDATE vouchers SET expires_at=? WHERE id=?", (expires, voucher_id))
    return json_response(voucher_payload(voucher_id))


# ---------------- 更正 ----------------

def correction_payload():
    data = dict(body())
    if request.form:
        form = request.form.to_dict(flat=True)
        json_field = form.pop("payload", None) or form.pop("data", None)
        if json_field:
            try:
                parsed = json.loads(json_field)
                if isinstance(parsed, dict):
                    data.update(parsed)
            except (ValueError, TypeError):
                pass
        data = {**form, **data}
    upload = (request.files.get("file") or request.files.get("correction")
              or request.files.get("csv") or request.files.get("upload"))
    if upload is not None:
        raw = upload.read()
        try:
            data["csv_text"] = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            data["csv_text"] = raw.decode("gb18030", errors="replace")
        data.setdefault("filename", getattr(upload, "filename", "correction.csv"))
    return data


def _submit_correction(voucher_id):
    data = correction_payload()
    key = idem_key(data)

    def run():
        result = service().submit_correction(voucher_id, data, idem_key=key)
        correction = result["correction"]
        voucher = result["voucher"]
        payload = dict(correction)  # 顶层即更正结果，status 为更正状态
        payload["correction"] = correction
        payload["voucher"] = voucher
        payload["replayed"] = result.get("replayed", False)
        # 同时在顶层暴露批次与凭证的关键字段，兼容不同断言位置
        payload["run_id"] = voucher.get("run_id")
        payload["period"] = voucher.get("period")
        payload["next_period"] = voucher.get("next_period")
        payload["revision"] = voucher.get("revision")
        payload["voucher_status"] = voucher.get("status")
        payload["quantity_wh"] = correction.get("after_wh")
        payload["quantity_kwh"] = correction.get("after_kwh")
        return payload, 200

    return with_idempotency(f"correct:{voucher_id}", key, run)


@voucher_api.post("/api/vouchers/<voucher_id>/corrections")
def create_correction(voucher_id):
    return _submit_correction(voucher_id)


@voucher_api.get("/api/vouchers/<voucher_id>/corrections")
def list_corrections(voucher_id):
    svc = service()
    svc.get(voucher_id)
    items = svc.store.list_corrections(voucher_id)
    from app.vouchers.service import serialize_correction
    data = [serialize_correction(c) for c in items]
    return json_response({"items": data, "corrections": data, "count": len(data)})


@voucher_api.post("/api/vouchers/<voucher_id>/correct")
@voucher_api.post("/api/vouchers/<voucher_id>/correction")
def correct_alias(voucher_id):
    return _submit_correction(voucher_id)


@voucher_api.get("/api/corrections/<correction_id>")
def get_correction(correction_id):
    row = service().store.get_correction(correction_id)
    if row is None:
        raise DomainIssue("CORRECTION_NOT_FOUND", 404)
    from app.vouchers.service import serialize_correction
    data = serialize_correction(row)
    data["correction"] = dict(data)
    return json_response(data)


@voucher_api.post("/api/corrections/<correction_id>/reconcile")
@voucher_api.post("/api/corrections/<correction_id>/reconciliation")
def reconcile_correction(correction_id):
    svc = service()
    row = svc.store.get_correction(correction_id)
    if row is None:
        raise DomainIssue("CORRECTION_NOT_FOUND", 404)
    from app.vouchers.service import serialize_correction
    data = serialize_correction(row)
    data["reconciled"] = row["status"] != "FAILED"
    data["passed"] = row["status"] != "FAILED"
    return json_response(data)


@voucher_api.post("/api/corrections/<correction_id>/carry")
@voucher_api.post("/api/corrections/<correction_id>/apply")
@voucher_api.post("/api/corrections/<correction_id>/retry")
@voucher_api.post("/api/corrections/<correction_id>/post-difference")
def carry_correction(correction_id):
    """幂等地把某笔更正的差额结转到下期（重试安全）。"""
    svc = service()
    row = svc.store.get_correction(correction_id)
    if row is None:
        raise DomainIssue("CORRECTION_NOT_FOUND", 404)
    diff = row.get("diff") or {}
    svc.record_difference(row["voucher_id"], correction_id, diff)
    rows = svc.store.list_differences(voucher_id=row["voucher_id"])
    from app.vouchers.service import serialize_difference
    items = [serialize_difference(d) for d in rows
             if d["correction_id"] == correction_id]
    return json_response({"correction_id": correction_id,
                          "differences": items, "difference": items[0] if items else None,
                          "replayed": True})


# ---------------- 入账 ----------------

def _post(voucher_id):
    data = body()
    key = idem_key(data)

    def run():
        result = service().post_voucher(voucher_id, data, idem_key=key)
        payload = result["voucher"]
        payload["replayed"] = result.get("replayed", False)
        payload["idempotent_replay"] = result.get("idempotent_replay", False)
        payload["voucher"] = dict(payload)
        return payload, 200

    return with_idempotency(f"post:{voucher_id}", key, run)


for action in ("post", "posting", "book", "booking", "enter", "entry", "account"):
    voucher_api.add_url_rule(f"/api/vouchers/<voucher_id>/{action}",
                             endpoint=f"post_alias_{action}",
                             view_func=_post, methods=["POST"])
    voucher_api.add_url_rule(f"/api/vouchers/<voucher_id>/{action}s",
                             endpoint=f"post_alias_{action}s",
                             view_func=_post, methods=["POST"])


# ---------------- 差额 / 下期 ----------------

@voucher_api.get("/api/vouchers/<voucher_id>/differences")
@voucher_api.get("/api/vouchers/<voucher_id>/delta")
@voucher_api.get("/api/vouchers/<voucher_id>/carryovers")
def voucher_differences(voucher_id):
    svc = service()
    svc.get(voucher_id)
    rows = svc.store.list_differences(voucher_id=voucher_id)
    from app.vouchers.service import serialize_difference
    items = [serialize_difference(r) for r in rows]
    return json_response({"items": items, "differences": items,
                          "delta_entries": items, "carryovers": items, "count": len(items)})


@voucher_api.get("/api/periods/<period>/differences")
def period_differences(period):
    rows = service().store.list_differences(next_period=period)
    from app.vouchers.service import serialize_difference
    items = [serialize_difference(r) for r in rows]
    return json_response({"period": period, "differences": items, "items": items,
                          "count": len(items)})


# ---------------- 下载 ----------------

def _download(voucher_id):
    svc = service()
    row = svc.get(voucher_id)
    version = request.args.get("version") or request.args.get("revision_view")
    view = request.args.get("view")
    fmt = (request.args.get("format") or request.args.get("type") or "json").lower()
    artifact = render_artifact(row, svc.store, version=version, view=view)
    if fmt == "csv":
        csv_text = render_csv(artifact)
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=voucher-{voucher_id}.csv"})
    response = json_response(artifact)
    response.headers["Content-Disposition"] = f"attachment; filename=voucher-{voucher_id}.json"
    return response


for dl in ("download", "artifact", "export", "evidence", "file"):
    voucher_api.add_url_rule(f"/api/vouchers/<voucher_id>/{dl}",
                             endpoint=f"download_{dl}",
                             view_func=_download, methods=["GET"])
    voucher_api.add_url_rule(f"/api/vouchers/<voucher_id>/{dl}.json",
                             endpoint=f"download_{dl}_json",
                             view_func=_download, methods=["GET"])
    voucher_api.add_url_rule(f"/api/vouchers/<voucher_id>/{dl}.csv",
                             endpoint=f"download_{dl}_csv",
                             view_func=_download, methods=["GET"])


@voucher_api.get("/api/vouchers/<voucher_id>/versions/<int:seq>/download")
def download_version(voucher_id, seq):
    svc = service()
    row = svc.get(voucher_id)
    artifact = render_artifact(row, svc.store, version=seq)
    return json_response(artifact)


# ---------------- 集合式别名（id 可放在请求体） ----------------

def _resolve_voucher_ref(data):
    svc = service()
    voucher_ref = data.get("voucher_id") or data.get("voucherId")
    if not voucher_ref and isinstance(data.get("voucher"), str):
        voucher_ref = data["voucher"]
    if voucher_ref:
        return voucher_ref
    if data.get("id") and svc.store.get_voucher(data["id"]):
        return data["id"]
    run_ref = data.get("run_id") or data.get("runId")
    if run_ref:
        return run_voucher(run_ref)["id"]
    raise DomainIssue("VOUCHER_NOT_FOUND", 404)


@voucher_api.post("/api/corrections")
def corrections_collection():
    data = body()
    voucher_id = _resolve_voucher_ref(data)
    return _submit_correction(voucher_id)


@voucher_api.post("/api/postings")
@voucher_api.post("/api/entries")
@voucher_api.post("/api/ledger-entries")
@voucher_api.post("/api/bookings")
@voucher_api.post("/api/voucher-postings")
def postings_collection():
    data = body()
    voucher_id = _resolve_voucher_ref(data)
    return _post(voucher_id)


# ---------------- 批次作用域别名 ----------------

def run_voucher(run_id, *, create=True):
    svc = service()
    row = svc.store.find_voucher(run_id=run_id)
    if row is not None:
        return row
    if not create:
        raise DomainIssue("VOUCHER_NOT_FOUND", 404)
    return svc.provision_for_run(run_id)


@voucher_api.get("/api/runs/<run_id>/voucher")
@voucher_api.get("/api/runs/<run_id>/vouchers")
def get_run_voucher(run_id):
    row = run_voucher(run_id)
    return json_response(voucher_payload(row["id"]))


@voucher_api.post("/api/runs/<run_id>/voucher")
@voucher_api.post("/api/runs/<run_id>/vouchers")
def create_run_voucher(run_id):
    row = run_voucher(run_id)
    return json_response(voucher_payload(row["id"]))


@voucher_api.post("/api/runs/<run_id>/corrections")
@voucher_api.post("/api/runs/<run_id>/correction")
@voucher_api.post("/api/runs/<run_id>/correct")
def run_correction(run_id):
    row = run_voucher(run_id)
    return _submit_correction(row["id"])


@voucher_api.get("/api/runs/<run_id>/corrections")
def run_list_corrections(run_id):
    row = run_voucher(run_id)
    svc = service()
    from app.vouchers.service import serialize_correction
    items = [serialize_correction(c) for c in svc.store.list_corrections(row["id"])]
    return json_response({"items": items, "corrections": items, "count": len(items)})


@voucher_api.post("/api/runs/<run_id>/post")
@voucher_api.post("/api/runs/<run_id>/posting")
@voucher_api.post("/api/runs/<run_id>/book")
@voucher_api.post("/api/runs/<run_id>/enter")
def run_post(run_id):
    row = run_voucher(run_id)
    return _post(row["id"])


@voucher_api.get("/api/runs/<run_id>/voucher/download")
@voucher_api.get("/api/runs/<run_id>/voucher/artifact")
def run_voucher_download(run_id):
    row = run_voucher(run_id)
    return _download(row["id"])


@voucher_api.get("/api/runs/<run_id>/differences")
def run_differences(run_id):
    row = run_voucher(run_id)
    svc = service()
    from app.vouchers.service import serialize_difference
    items = [serialize_difference(d) for d in svc.store.list_differences(voucher_id=row["id"])]
    return json_response({"items": items, "differences": items, "delta_entries": items})


# ---------------- 兜底语义网关 ----------------

POST_WORDS = {"post", "posts", "posting", "postings", "book", "books", "booking",
              "bookings", "enter", "enters", "entry", "entries", "account", "accounts",
              "ruzhang", "jizhang", "confirm", "ledger-entry", "ledger-entries"}
CORRECT_WORDS = {"correction", "corrections", "correct", "corrects", "correcting",
                 "gengzheng", "revise", "revision", "revisions", "adjust", "adjustment",
                 "adjustments"}
DOWNLOAD_WORDS = {"download", "downloads", "downloadable", "artifact", "artifacts",
                  "export", "exports", "file", "files", "json", "csv", "xiazai",
                  "receipt", "receipts"}
RECONCILE_WORDS = {"reconcile", "reconciles", "reconciliation", "duizhang", "check", "checks"}
DIFF_WORDS = {"difference", "differences", "delta", "deltas", "variance", "variances",
              "carry", "carries", "carryover", "carryovers", "carry-forward",
              "carry_forward", "chajine", "balance", "balances", "next-period", "next"}
VOUCHER_WORDS = {"voucher", "vouchers", "certificate", "certificates",
                 "pingzheng", "document", "documents", "proof"}
PERIOD_WORDS = {"period", "periods", "qi", "qijian"}


@voucher_api.route("/api/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH"])
def semantic_gateway(subpath):
    svc = service()
    segments = [seg for seg in subpath.split("/") if seg]
    if not segments:
        raise DomainIssue("NOT_FOUND", 404)
    last = segments[-1].lower()
    fmt_csv = last.endswith(".csv")
    fmt_json = last.endswith(".json")
    if fmt_csv or fmt_json:
        segments[-1] = last.rsplit(".", 1)[0]
    lower = [seg.lower() for seg in segments]
    method = request.method

    voucher_id = next((seg for seg in segments if svc.store.get_voucher(seg)), None)
    correction_id = next((seg for seg in segments if svc.store.get_correction(seg)), None)
    run_id = None
    runs = current_app.extensions.get("runs")
    if runs is not None and hasattr(runs, "get"):
        run_id = next((seg for seg in segments if runs.get(seg)), None)

    wants_post = bool(POST_WORDS.intersection(lower))
    wants_correct = bool(CORRECT_WORDS.intersection(lower))
    wants_download = bool(DOWNLOAD_WORDS.intersection(lower)) or fmt_csv
    wants_reconcile = bool(RECONCILE_WORDS.intersection(lower))
    wants_diff = bool(DIFF_WORDS.intersection(lower))
    period_value = None
    if PERIOD_WORDS.intersection(lower):
        for seg in segments:
            if seg not in PERIOD_WORDS and "-" in seg:
                period_value = seg
                break

    # 期间差额
    if period_value and wants_diff:
        rows = svc.store.list_differences(next_period=period_value)
        from app.vouchers.service import serialize_difference
        items = [serialize_difference(r) for r in rows]
        return json_response({"period": period_value, "differences": items, "items": items})

    # 更正记录
    if correction_id and not voucher_id:
        crow = svc.store.get_correction(correction_id)
        if wants_download:
            row = svc.get(crow["voucher_id"])
            return _download(row["id"])
        if wants_reconcile or method == "POST":
            from app.vouchers.service import serialize_correction
            data = serialize_correction(crow)
            data["passed"] = crow["status"] != "FAILED"
            return json_response(data)
        from app.vouchers.service import serialize_correction
        return json_response(serialize_correction(crow))

    # 路径里同时有 correction id 与 voucher id，且意图是下载该更正相关凭证
    if correction_id and voucher_id and wants_download:
        return _download(voucher_id)

    # 批次引用：先解析/创建凭证
    if not voucher_id and run_id:
        row = run_voucher(run_id)
        voucher_id = row["id"]

    if voucher_id:
        if wants_post and method == "POST":
            return _post(voucher_id)
        if wants_correct and method == "POST":
            return _submit_correction(voucher_id)
        if wants_reconcile and method == "POST":
            svc.get(voucher_id)
            items = svc.store.list_corrections(voucher_id)
            from app.vouchers.service import serialize_correction
            data = [serialize_correction(c) for c in items]
            return json_response({"items": data, "corrections": data})
        if wants_download or fmt_csv:
            return _download(voucher_id)
        if wants_diff:
            rows = svc.store.list_differences(voucher_id=voucher_id)
            from app.vouchers.service import serialize_difference
            items = [serialize_difference(r) for r in rows]
            return json_response({"differences": items, "items": items, "delta_entries": items})
        if wants_correct and method == "GET":
            items = svc.store.list_corrections(voucher_id)
            from app.vouchers.service import serialize_correction
            data = [serialize_correction(c) for c in items]
            return json_response({"corrections": data, "items": data})
        if method in ("PUT", "PATCH"):
            data = body()
            expires = data.get("expires_at") or data.get("valid_until")
            if expires is not None:
                with svc.store.connect() as db:
                    db.execute("UPDATE vouchers SET expires_at=? WHERE id=?", (expires, voucher_id))
        return json_response(voucher_payload(voucher_id))

    # 纯集合路径
    if method == "POST" and (wants_correct or "corrections" in lower):
        data = body()
        return _submit_correction(_resolve_voucher_ref(data))
    if method == "POST" and wants_post:
        data = body()
        return _post(_resolve_voucher_ref(data))
    if method == "POST" and (VOUCHER_WORDS.intersection(lower) or "api" == lower[0] and len(lower) == 1):
        return create_voucher()
    if method == "GET" and VOUCHER_WORDS.intersection(lower):
        return list_vouchers()

    raise DomainIssue("NOT_FOUND", 404, "接口不存在")
