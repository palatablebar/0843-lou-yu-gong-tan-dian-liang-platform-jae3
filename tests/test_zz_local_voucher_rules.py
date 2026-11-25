"""更正 / 凭证 / 入账 / 下期差额 的验收测试。

对应需求：
- 入账前采用对账通过的更正，当期凭证尽快反映正确电量；
- 已入账凭证原样留存，入账后更正只把差额计入下期；
- 更正与入账并发时以入账是否生效为准，过期凭证不能再入账；
- 对账失败保留旧版供下载；
- 重试不重复记差额；
- 下载件能对回更正前后电量。
"""
import threading

import pytest

from app.vouchers.service import VoucherService


def approved_run(client, profile="clean"):
    run = client.post("/api/runs", json={"profile": profile}).get_json()
    resp = client.post(f"/api/runs/{run['run_id']}/review",
                       json={"decision": "APPROVED", "comment": "复核完成",
                             "expected_revision": 0})
    assert resp.status_code == 200
    return run


def voucher_for_run(client, run_id):
    resp = client.post(f"/api/runs/{run_id}/voucher", json={})
    assert resp.status_code == 200
    return resp.get_json()


# ---------- 建凭证 ----------

def test_voucher_seeded_from_clean_run_quantities(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    # clean 样例：总表 750 kWh = 750000 Wh；分表 600；公摊 150
    assert v["quantity_wh"] == 750000
    assert v["main_wh"] == 750000
    assert v["usage_wh"] == 600000
    assert v["common_wh"] == 150000
    assert v["status"] in {"OPEN", "DRAFT", "ISSUED"}
    assert v["revision"] == 0


def test_can_create_standalone_voucher_with_kwh(client):
    resp = client.post("/api/vouchers", json={"period": "2026-11", "quantity_kwh": 12.5})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["quantity_wh"] == 12500
    assert data["next_period"] == "2026-12"


# ---------- 入账前更正 ----------

def test_passing_correction_before_post_is_adopted(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    resp = client.post(f"/api/vouchers/{v['id']}/corrections",
                       json={"quantity_kwh": 762, "reason": "表计更正"})
    assert resp.status_code == 200
    corr = resp.get_json()
    assert corr["status"] in {"APPLIED", "RECONCILED", "PASS"}
    assert corr["before_wh"] == 750000
    assert corr["after_wh"] == 762000
    assert corr["difference_wh"] == 12000

    again = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert again["quantity_wh"] == 762000
    assert again["version"] >= 2


def test_unbalanced_correction_fails_reconciliation(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    resp = client.post(f"/api/vouchers/{v['id']}/corrections",
                       json={"main_kwh": 100, "usage_kwh": 90, "common_kwh": 20})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] in {"RECONCILE_FAILED", "CORRECTION_RECONCILE_FAILED", "GATE_BLOCKED"}
    assert any(not c["ok"] for c in body["reconciliation"]["checks"])

    kept = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert kept["quantity_wh"] == 750000


def test_explicit_failed_reconciliation_keeps_old_version(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    resp = client.post(f"/api/vouchers/{v['id']}/corrections",
                       json={"quantity_kwh": 999, "reconciled": False})
    assert resp.status_code == 409
    kept = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert kept["quantity_wh"] == 750000
    assert any(c["status"] == "FAILED" for c in kept["corrections"])


# ---------- 过期版本 ----------

def test_stale_voucher_cannot_post(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    # 一笔更正把 revision 推到 1
    ok = client.post(f"/api/vouchers/{v['id']}/corrections", json={"quantity_kwh": 760})
    assert ok.status_code == 200
    stale = client.post(f"/api/vouchers/{v['id']}/post", json={"expected_revision": 0})
    assert stale.status_code == 409
    assert stale.get_json()["code"] in {"STALE_VOUCHER", "VOUCHER_STALE", "VOUCHER_EXPIRED"}
    # 用当前版本可以入账
    current = client.get(f"/api/vouchers/{v['id']}").get_json()
    good = client.post(f"/api/vouchers/{v['id']}/post",
                       json={"expected_revision": current["revision"]})
    assert good.status_code == 200
    assert good.get_json()["status"] == "POSTED"


def test_time_expired_voucher_cannot_post(client):
    v = client.post("/api/vouchers",
                    json={"quantity_kwh": 10, "expires_at": "2001-01-01T00:00:00+00:00"}).get_json()
    resp = client.post(f"/api/vouchers/{v['id']}/post", json={})
    assert resp.status_code == 409
    assert resp.get_json()["code"] in {"VOUCHER_EXPIRED", "EXPIRED_VOUCHER", "STALE_VOUCHER"}


# ---------- 入账后：冻结 + 差额下期 ----------

def test_posted_voucher_frozen_and_delta_carried_to_next_period(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    posted = client.post(f"/api/vouchers/{v['id']}/post",
                         json={"expected_revision": 0}).get_json()
    assert posted["status"] == "POSTED"
    assert posted["posted_quantity_wh"] == 750000

    resp = client.post(f"/api/vouchers/{v['id']}/corrections",
                       json={"quantity_kwh": 758, "idempotency_key": "corr-1"})
    assert resp.status_code == 200
    corr = resp.get_json()
    assert corr["status"] in {"CARRIED", "POST_POST", "DEFERRED"}
    assert corr["difference_wh"] == 8000

    frozen = client.get(f"/api/vouchers/{v['id']}").get_json()
    # 已入账凭证原样留存：当期表面与入账快照都不变
    assert frozen["quantity_wh"] == 750000
    assert frozen["posted_quantity_wh"] == 750000
    assert len(frozen["differences"]) == 1
    diff = frozen["differences"][0]
    assert diff["wh"] == 8000
    assert diff["next_period"] == frozen["next_period"]

    period_resp = client.get(f"/api/periods/{frozen['next_period']}/differences").get_json()
    assert period_resp["count"] == 1


# ---------- 重试幂等 ----------

def test_retry_with_same_idempotency_key_does_not_duplicate_delta(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    client.post(f"/api/vouchers/{v['id']}/post", json={"expected_revision": 0})
    for _ in range(3):
        resp = client.post(f"/api/vouchers/{v['id']}/corrections",
                           json={"quantity_kwh": 758, "idempotency_key": "same-corr"})
        assert resp.status_code == 200
    data = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert len(data["differences"]) == 1
    assert sum(d["wh"] for d in data["differences"]) == 8000


def test_content_retry_without_key_is_also_deduped(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    client.post(f"/api/vouchers/{v['id']}/post", json={"expected_revision": 0})
    payload = {"quantity_kwh": 758, "reason": "same retry"}
    first = client.post(f"/api/vouchers/{v['id']}/corrections", json=payload)
    second = client.post(f"/api/vouchers/{v['id']}/corrections", json=payload)
    assert first.status_code == second.status_code == 200
    data = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert len(data["corrections"]) == 1
    assert len(data["differences"]) == 1


def test_post_retry_with_idempotency_key_is_safe(client):
    v = client.post("/api/vouchers", json={"quantity_kwh": 100}).get_json()
    responses = [
        client.post(f"/api/vouchers/{v['id']}/post",
                    json={"idempotency_key": "post-key"}).status_code
        for _ in range(3)
    ]
    assert all(code == 200 for code in responses)
    data = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert data["status"] == "POSTED"


# ---------- 下载 / 对账 ----------

def test_download_reconciles_before_and_after_quantities(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    client.post(f"/api/vouchers/{v['id']}/corrections", json={"quantity_kwh": 762})
    artifact = client.get(f"/api/vouchers/{v['id']}/download").get_json()
    assert artifact["schema"] == "workbench-voucher/v1"
    # 原始 750、更正后 762 都能在下载件顶层对回
    assert artifact["original_quantity_wh"] == 750000
    assert artifact["corrected_quantity_wh"] == 762000
    assert artifact["difference_wh"] == 12000
    assert artifact["before_wh"] == 750000
    assert artifact["after_wh"] == 762000
    assert len(artifact["artifact_sha256"]) == 64
    # 版本链包含 ORIGINAL 与 CORRECTED
    kinds = [ver["kind"] for ver in artifact["versions"]]
    assert "ORIGINAL" in kinds and "CORRECTED" in kinds


def test_failed_correction_old_version_still_downloadable(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    bad = client.post(f"/api/vouchers/{v['id']}/corrections",
                      json={"quantity_kwh": -5, "reconciled": True})
    assert bad.status_code in {400, 409}
    resp = client.get(f"/api/vouchers/{v['id']}/download")
    assert resp.status_code == 200
    artifact = resp.get_json()
    assert artifact["quantity_wh"] == 750000
    # 原始版本仍可按版本号下载
    v1 = client.get(f"/api/vouchers/{v['id']}/versions/1/download")
    assert v1.status_code == 200
    assert v1.get_json()["quantity_wh"] == 750000


def test_posted_download_shows_posted_snapshot_and_carried_after(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    client.post(f"/api/vouchers/{v['id']}/post", json={"expected_revision": 0})
    client.post(f"/api/vouchers/{v['id']}/corrections", json={"quantity_kwh": 770})
    art = client.get(f"/api/vouchers/{v['id']}/download").get_json()
    # 已入账快照仍是 750
    assert art["posted"]["quantity_wh"] == 750000
    assert art["quantity_wh"] == 750000
    # 但更正前 750 / 更正后 770 与差额 20000 可对回
    assert art["before_wh"] == 750000
    assert art["after_wh"] == 770000
    assert art["difference"]["carried_wh"] == 20000


def test_download_csv_format(client):
    v = client.post("/api/vouchers", json={"quantity_kwh": 100}).get_json()
    resp = client.get(f"/api/vouchers/{v['id']}/download?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert b"difference_wh" in resp.data or b"750" in resp.data or b"100000" in resp.data


# ---------- 并发 ----------

def test_concurrent_identical_correction_records_single_delta(client):
    from concurrent.futures import ThreadPoolExecutor
    v = client.post("/api/vouchers", json={"quantity_kwh": 100}).get_json()
    client.post(f"/api/vouchers/{v['id']}/post", json={"expected_revision": 0})
    barrier = threading.Barrier(6)

    def fire(_):
        cc = client.application.test_client()
        barrier.wait(timeout=10)
        return cc.post(f"/api/vouchers/{v['id']}/corrections",
                        json={"quantity_kwh": 130, "idempotency_key": "race-key"}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(fire, range(6)))
    assert all(code == 200 for code in codes), codes
    data = client.get(f"/api/vouchers/{v['id']}").get_json()
    assert len(data["differences"]) == 1
    assert data["differences"][0]["wh"] == 30000


def test_concurrent_post_and_correction_are_consistent(client):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    barrier = threading.Barrier(2)
    results = {}

    def do_post():
        cc = client.application.test_client()
        barrier.wait(timeout=10)
        results["post"] = cc.post(f"/api/vouchers/{v['id']}/post",
                                  json={"expected_revision": 0}).status_code

    def do_correct():
        cc = client.application.test_client()
        barrier.wait(timeout=10)
        results["correct"] = cc.post(f"/api/vouchers/{v['id']}/corrections",
                                     json={"quantity_kwh": 760,
                                           "idempotency_key": "race-corr"}).status_code

    t1 = threading.Thread(target=do_post)
    t2 = threading.Thread(target=do_correct)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 以入账是否生效为准：两种串行化结果都合法，但不能两个都被拒绝或出现 5xx
    assert results["post"] in (200, 409)
    assert results["correct"] == 200
    data = client.get(f"/api/vouchers/{v['id']}").get_json()
    if results["post"] == 200:
        assert data["status"] == "POSTED"
        # 入账已生效：更正体现为一笔且仅一笔下期差额，入账快照保持 750
        assert len(data["differences"]) == 1
        assert data["differences"][0]["wh"] == 10000
        assert data["posted_quantity_wh"] == 750000
    else:
        # 更正先生效：当期凭证为 760，旧版本入账请求被拒（过期凭证不能入账）
        assert results["post"] == 409
        assert data["status"] != "POSTED"
        assert data["quantity_wh"] == 760000
        assert data["differences"] == []
        refreshed = client.post(f"/api/vouchers/{v['id']}/post",
                                json={"expected_revision": data["revision"]})
        assert refreshed.status_code == 200
        assert refreshed.get_json()["posted_quantity_wh"] == 760000


# ---------- 路径别名 ----------

@pytest.mark.parametrize("path_builder", [
    lambda rid, vid: f"/api/runs/{rid}/corrections",
    lambda rid, vid: f"/api/runs/{rid}/correct",
    lambda rid, vid: f"/api/corrections",
])
def test_correction_endpoint_aliases(client, path_builder):
    run = approved_run(client)
    v = voucher_for_run(client, run["run_id"])
    path = path_builder(run["run_id"], v["id"])
    payload = {"quantity_kwh": 755}
    if path == "/api/corrections":
        payload["voucher_id"] = v["id"]
    resp = client.post(path, json=payload)
    assert resp.status_code == 200, path


def test_singular_voucher_alias_and_gateway(client):
    v = client.post("/api/vouchers", json={"quantity_kwh": 42}).get_json()
    for path in (f"/api/voucher/{v['id']}", f"/api/vouchers/{v['id']}/artifact",
                 f"/api/vouchers/{v['id']}/book"):
        if path.endswith("/book"):
            resp = client.post(path, json={})
        else:
            resp = client.get(path)
        assert resp.status_code == 200, path


# ---------- 服务层 ----------

def test_service_layer_idempotent_difference(tmp_path):
    db = str(tmp_path / "s.sqlite3")
    svc = VoucherService(db)
    row = svc.create_voucher({"quantity_kwh": 10})
    svc.post_voucher(row["id"], {})
    result = svc.submit_correction(row["id"], {"quantity_kwh": 14})
    cid = result["correction"]["id"]
    diff = {"difference_wh": 4000, "primary_field": "quantity_wh", "amounts_wh": {"quantity_wh": 4000}}
    svc.record_difference(row["id"], cid, diff)
    svc.record_difference(row["id"], cid, diff)
    rows = svc.store.list_differences(voucher_id=row["id"])
    assert len(rows) == 1
    assert rows[0]["wh"] == 4000
