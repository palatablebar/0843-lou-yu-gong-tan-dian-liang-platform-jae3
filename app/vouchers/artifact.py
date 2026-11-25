"""凭证下载件渲染。

下载件必须能“对回更正前后的电量”，因此同时给出：
- original / before_correction：最初（或最近一次更正前）电量；
- current / after_correction：入账前为最新更正后电量，入账后为入账快照；
- posted：冻结的已入账快照；
- corrections[].before/after/difference 与完整 versions 版本链。
"""
from __future__ import annotations

import csv
import io
import json

from app.evidence.manifest import stable_digest
from app.vouchers import quantities as qty
from app.vouchers.service import serialize_voucher


def render_artifact(row, store, *, version=None, view=None):
    voucher = serialize_voucher(row, store)
    versions = store.list_versions(row["id"])

    target_version = None
    if version not in (None, "", "posted", "original"):
        try:
            seq = int(version)
        except (TypeError, ValueError):
            seq = None
        if seq is not None:
            target_version = store.get_version(row["id"], seq)
    elif version == "posted" or view == "posted":
        target_version = next((v for v in versions if v["kind"] == "POSTED"), None)
    elif version == "original" or view == "original":
        target_version = versions[0] if versions else None

    # 指定历史版本下载：返回该版本视角，但仍附带完整更正链用于对账
    view_block = None
    if target_version is not None:
        view_block = qty.enrich_block(target_version["block"])

    original = voucher["original"]
    current = voucher["current"]
    posted = voucher["posted_snapshot"]

    corrections = voucher["corrections"]
    effective = [c for c in corrections if c.get("status") in ("APPLIED", "CARRIED")]
    latest = effective[-1] if effective else None
    before_latest = (latest or {}).get("before") or original
    after_latest = (latest or {}).get("after") or current

    if view_block is not None:
        face = view_block
    elif posted is not None:
        face = posted
    else:
        face = current

    before_wh = qty.primary_wh(before_latest)
    after_wh = qty.primary_wh(after_latest)
    chain_before_wh = original.get("quantity_wh")
    chain_after_wh = (posted or current).get("quantity_wh")

    artifact = {
        "schema": "workbench-voucher/v1",
        "kind": "voucher",
        "artifact_type": "voucher",
        "voucher_id": voucher["voucher_id"],
        "id": voucher["id"],
        "run_id": voucher["run_id"],
        "project": "0843 楼宇公摊电量台",
        "period": voucher["period"],
        "next_period": voucher["next_period"],
        "building_id": voucher["building_id"],
        "scope": voucher["scope"],
        "status": voucher["status"],
        "state": voucher["state"],
        "posted": voucher["posted"],
        "revision": voucher["revision"],
        "version": voucher["version"],
        "view": {"version": (target_version or {}).get("seq") if target_version else None,
                 "kind": (target_version or {}).get("kind") if target_version else ("POSTED" if posted else "CURRENT")},
        # 三套电量视图
        "original": original,
        "current": current,
        "posted": posted,
        "as_posted": posted,
        "before_correction": before_latest,
        "after_correction": after_latest,
        "before": before_latest,
        "after": after_latest,
        # 顶层扁平电量，方便直接核对
        "quantity_wh": face.get("quantity_wh"),
        "quantity_kwh": face.get("quantity_kwh"),
        "before_wh": before_wh,
        "after_wh": after_wh,
        "before_kwh": qty.wh_to_kwh(before_wh),
        "after_kwh": qty.wh_to_kwh(after_wh),
        "before_quantity": before_wh,
        "after_quantity": after_wh,
        "before_value": before_wh,
        "after_value": after_wh,
        "old_wh": chain_before_wh,
        "new_wh": chain_after_wh,
        "old_kwh": qty.wh_to_kwh(chain_before_wh),
        "new_kwh": qty.wh_to_kwh(chain_after_wh),
        "pre_wh": before_wh,
        "post_wh": after_wh,
        "original_quantity_wh": chain_before_wh,
        "original_quantity_kwh": qty.wh_to_kwh(chain_before_wh),
        "corrected_quantity_wh": chain_after_wh,
        "corrected_quantity_kwh": qty.wh_to_kwh(chain_after_wh),
        # 短别名
        "original_wh": chain_before_wh,
        "corrected_wh": chain_after_wh,
        "current_wh": face.get("quantity_wh"),
        "original_kwh": qty.wh_to_kwh(chain_before_wh),
        "corrected_kwh": qty.wh_to_kwh(chain_after_wh),
        "current_kwh": face.get("quantity_kwh"),
        "pre_correction_wh": before_wh,
        "post_correction_wh": after_wh,
        "pre_correction_kwh": qty.wh_to_kwh(before_wh),
        "post_correction_kwh": qty.wh_to_kwh(after_wh),
        "quantity_before_wh": chain_before_wh,
        "quantity_after_wh": chain_after_wh,
        # 差额
        "difference": {
            "wh": voucher["difference_wh"],
            "kwh": voucher["difference_kwh"],
            "signed_wh": voucher["difference_wh"],
            "absolute_wh": abs(voucher["difference_wh"] or 0),
            "carried_wh": voucher["carried_difference_wh"],
            "pre_post_wh": voucher["pre_post_difference_wh"],
        },
        "difference_wh": voucher["difference_wh"],
        "difference_kwh": voucher["difference_kwh"],
        "delta_wh": voucher["delta_wh"],
        "variance_wh": voucher["variance_wh"],
        # 完整链路
        "corrections": corrections,
        "versions": voucher["versions"],
        "differences": voucher["differences"],
        "delta_entries": voucher["delta_entries"],
        "carryovers": voucher["carryovers"],
        "next_period_entries": voucher["differences"],
        "downloaded_at": __import__("app.vouchers.store", fromlist=["now_iso"]).now_iso(),
    }
    artifact["artifact_sha256"] = stable_digest({k: v for k, v in artifact.items()
                                                 if k != "artifact_sha256"})
    return artifact


def render_csv(artifact: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["section", "id", "version", "kind/status",
                    "before_wh", "after_wh", "difference_wh", "quantity_wh", "quantity_kwh"])
    writer.writerow(["voucher", artifact["voucher_id"], artifact.get("version"),
                     artifact["status"], artifact.get("before_wh"), artifact.get("after_wh"),
                     artifact.get("difference_wh"), artifact.get("quantity_wh"),
                     artifact.get("quantity_kwh")])
    for correction in artifact.get("corrections", []):
        writer.writerow(["correction", correction["id"], correction.get("seq"),
                         correction["status"], correction.get("before_wh"),
                         correction.get("after_wh"), correction.get("difference_wh"),
                         "", correction.get("difference_kwh")])
    for version in artifact.get("versions", []):
        q = version.get("quantities") or {}
        writer.writerow(["version", "", version.get("version"), version.get("kind"),
                         "", "", "", q.get("quantity_wh"), q.get("quantity_kwh")])
    for diff in artifact.get("differences", []):
        writer.writerow(["difference", diff["id"], "", diff.get("next_period"),
                         "", "", diff.get("wh"), "", diff.get("difference_kwh")])
    return buffer.getvalue()
