from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    artifact = render(run)
    # 若批次已衍生凭证/更正，把凭证链并入证据，便于“对回更正前后电量”。
    try:
        voucher_row = store().find_voucher(run_id=run_id) if hasattr(store(), "find_voucher") else None
        if voucher_row is not None:
            from app.evidence.manifest import stable_digest
            from app.vouchers.service import serialize_voucher
            voucher = serialize_voucher(voucher_row, store())
            artifact["voucher"] = voucher
            artifact["voucher_id"] = voucher["id"]
            artifact["corrections"] = voucher.get("corrections", [])
            artifact["differences"] = voucher.get("differences", [])
            artifact["voucher_download_url"] = voucher.get("download_url")
            artifact["artifact_sha256"] = stable_digest(
                {k: v for k, v in artifact.items() if k != "artifact_sha256"})
    except DomainIssue:
        raise
    except Exception:
        # 凭证扩展失败不影响既有签发证据
        pass
    return jsonify(artifact)


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
