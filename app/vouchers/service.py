"""凭证 / 更正 / 入账 / 下期差额的应用服务。

业务不变量（对应任务验收点）：

1. 入账前，对账通过的更正生成新的当前凭证版本，当期凭证立即反映正确电量；
2. 入账后，已入账快照冻结，任何更正都不再改动它，只把差额结转到下期；
3. 更正与入账在 ``BEGIN IMMEDIATE`` 事务内竞争，以入账是否已生效决定走向；
4. 过期（revision 不匹配或超过有效期）凭证不能再入账；
5. 对账失败的更正不落任何有效版本，旧版凭证原样保留、可下载；
6. 差额带确定性去重键，配合幂等键，重试绝不重复记账；
7. 下载件同时给出更正前/后电量与完整版本链，可对回。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.errors import DomainIssue
from app.evidence.manifest import stable_digest
from app.vouchers import quantities as qty
from app.vouchers.store import VoucherStore, new_id, now_iso

UNSET = object()


def _token(value):
    return None if value is UNSET else value

PERIOD_FMT = "%Y-%m"


def current_period(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime(PERIOD_FMT)


def next_period_of(period: str) -> str:
    year, month = (int(part) for part in period.split("-")[:2])
    month += 1
    if month == 13:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def block_from_run(run_row: dict) -> dict:
    """从批次 payload 提取电量块（整数 Wh）。"""
    payload = json.loads(run_row["payload"]) if isinstance(run_row.get("payload"), str) else (run_row.get("payload") or {})
    ledger = payload.get("ledger") or {}
    total_wh = None
    metric_total = ledger.get("metric_total")
    if metric_total not in (None, ""):
        try:
            total_wh = qty.parse_kwh_to_wh(Decimal(str(metric_total)))
        except Exception:
            total_wh = None
    usage_wh = common_wh = main_wh = 0
    found_components = False
    for row in payload.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("usage_mwh"), int):
            usage_wh += row["usage_mwh"]
            found_components = True
        if isinstance(row.get("common_mwh"), int):
            common_wh += row["common_mwh"]
        if isinstance(row.get("main_mwh"), int):
            main_wh += row["main_mwh"]
    if total_wh is None and main_wh:
        total_wh = main_wh
    if total_wh is None:
        main_kwh_total = 0.0
        for row in payload.get("rows", []) or []:
            if isinstance(row, dict) and row.get("main_kwh") is not None:
                try:
                    main_kwh_total += float(row["main_kwh"])
                except (TypeError, ValueError):
                    pass
        if main_kwh_total:
            total_wh = qty.parse_kwh_to_wh(main_kwh_total)
    block = qty.empty_block()
    block["quantity_wh"] = total_wh
    block["main_wh"] = total_wh or (main_wh or None)
    if found_components:
        block["usage_wh"] = usage_wh or None
        block["common_wh"] = common_wh or None
    return block


class VoucherService:
    def __init__(self, store=None, *, runs_store=None, database=None):
        if isinstance(store, VoucherStore):
            self.store = store
        elif store is not None and hasattr(store, "path") and hasattr(store, "list_vouchers"):
            # 已经是混入了凭证能力的 review Store
            self.store = store
        elif isinstance(store, str):
            self.store = VoucherStore(store)
        elif database:
            self.store = VoucherStore(database)
        else:
            self.store = VoucherStore(_default_db_path())
        self.runs_store = runs_store

    # ---------- 凭证的创建 / 获取 ----------

    def get(self, voucher_id):
        row = self.store.get_voucher(voucher_id)
        if row is None:
            raise DomainIssue("VOUCHER_NOT_FOUND", 404)
        return row

    def find_for_run(self, run_id):
        return self.store.find_voucher(run_id=run_id)

    def provision_for_run(self, run_id, *, period=None, expected_revision=UNSET):
        run_row = self._load_run(run_id)
        if run_row is None:
            raise DomainIssue("RUN_NOT_FOUND", 404)
        existing = self.store.find_voucher(run_id=run_id)
        if existing:
            return existing
        block = block_from_run(run_row)
        period = period or current_period()
        return self.create_voucher(
            run_id=run_id, period=period, current_block=block, original_block=dict(block),
            status="OPEN",
        )

    def create_voucher(self, data=None, **kwargs):
        data = data or {}
        run_id = kwargs.get("run_id", data.get("run_id"))
        period = kwargs.get("period") or data.get("period") or current_period()
        building_id = kwargs.get("building_id", data.get("building_id", "")) or ""
        scope = kwargs.get("scope", data.get("scope", "building-energy-split")) or "building-energy-split"

        # 仅在挂接批次时按自然键 get-or-create；独立凭证每次新建，
        # 避免 run_id 缺省时把不同凭证错误合并。
        existing = None
        if run_id is not None and not kwargs.get("force"):
            existing = self.store.find_voucher(run_id=run_id, period=period,
                                               building_id=building_id, scope=scope)
        if existing:
            return existing

        block = kwargs.get("current_block") or qty.normalize_block(data)
        if qty.primary_wh(block) is None and run_id:
            run_row = self._load_run(run_id)
            if run_row is not None:
                block = block_from_run(run_row)
        if qty.primary_wh(block) is None:
            # 允许空凭证（0 电量），但尽量从显式 total 取
            block["quantity_wh"] = 0
        original_block = kwargs.get("original_block") or dict(block)
        expires_at = data.get("expires_at") or data.get("valid_until") or data.get("expire_date")
        voucher_id = new_id("vch_")
        return self.store.insert_voucher(
            voucher_id=voucher_id, run_id=run_id, scope=scope, period=period,
            next_period=next_period_of(period), building_id=building_id,
            status=kwargs.get("status", "OPEN"), revision=0, version=1,
            current_block=block, original_block=original_block, expires_at=expires_at,
        )

    def get_or_create_for_run(self, run_id, data=None):
        existing = self.store.find_voucher(run_id=run_id)
        if existing:
            return existing
        return self.provision_for_run(run_id, period=(data or {}).get("period"))

    # ---------- 更正 ----------

    def submit_correction(self, voucher_id, payload: dict, *,
                          expected_revision=UNSET, expected_version=UNSET, idem_key=None):
        payload = payload or {}
        if expected_revision is UNSET:
            expected_revision = _pick_revision(payload)
        if expected_version is UNSET:
            expected_version = _pick_expected_version(payload)
        explicit_key = idem_key or payload.get("idempotency_key") or payload.get("idem_key") or payload.get("request_id")

        candidate = qty.normalize_block(payload)
        # 没有显式幂等键时，按“凭证 + 更正原始内容”生成确定性指纹，
        # 使原样重试（房屋既有 request_key 幂等风格）也不会重复记账。
        fingerprint = "fp:" + stable_digest({
            "voucher_id": voucher_id,
            "payload": _safe(payload),
        })[:24]
        idem_key = explicit_key or fingerprint

        existing_idem = self.store.find_correction_by_idem(voucher_id, idem_key)
        if existing_idem and existing_idem["status"] != "FAILED":
            return {"voucher": self.get(voucher_id), "correction": existing_idem, "replayed": True}

        dataset_report = self._dataset_report(payload)

        try:
            return self._execute_correction(
                voucher_id, payload, candidate, dataset_report,
                expected_revision, expected_version, idem_key)
        except sqlite3.IntegrityError:
            # 并发下相同幂等/指纹键已有成功更正：重放，不重复记差额
            winner = self.store.find_correction_by_idem(voucher_id, idem_key)
            if winner and winner["status"] != "FAILED":
                return {"voucher": self.get(voucher_id), "correction": winner, "replayed": True}
            raise

    def _execute_correction(self, voucher_id, payload, candidate, dataset_report,
                            expected_revision, expected_version, idem_key):
        with self.store.immediate() as db:
            row = db.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
            if row is None:
                raise DomainIssue("VOUCHER_NOT_FOUND", 404)
            voucher = dict(row)
            rev_mismatch = (expected_revision is not UNSET and expected_revision is not None
                            and int(expected_revision) != voucher["revision"])
            ver_mismatch = (expected_version is not UNSET and expected_version is not None
                            and int(expected_version) != voucher["version"])
            if (rev_mismatch or ver_mismatch) and voucher["status"] != "POSTED":
                # OPEN 状态下版本漂移只可能来自另一笔更正：拒绝覆盖，
                # 即“过期凭证/旧版本不能再提交”。若凭证已 POSTED，
                # 说明入账已生效，revision 漂移来自入账，此时以入账为准，
                # 本笔更正改走“差额结转下期”。
                err = DomainIssue("STALE_VOUCHER", 409, "凭证版本已过期，请刷新后再提交更正或入账")
                err.data = {"expected_revision": _token(expected_revision),
                            "current_revision": voucher["revision"],
                            "expected_version": _token(expected_version),
                            "current_version": voucher["version"], "expired": True}
                raise err

            current = json.loads(voucher["current_q"])
            # 纯差额/调整量载荷：after = 当前有效电量 + 带符号差额
            if not qty.has_absolute_quantity(payload):
                relative = qty.extract_relative_delta(payload)
                if relative is not None:
                    base = qty.primary_wh(current) or 0
                    candidate["quantity_wh"] = base + relative
            # 文件/明细重算对账：用管道结果电量兜底
            if qty.primary_wh(candidate) is None and dataset_report and dataset_report.get("quantity_wh") is not None:
                candidate["quantity_wh"] = dataset_report["quantity_wh"]
            report = qty.reconciliation(candidate, payload, current=current,
                                         dataset_report=dataset_report)
            seq_row = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM corrections WHERE voucher_id=?",
                (voucher_id,)).fetchone()
            seq = seq_row["s"]
            correction_id = new_id("cor_")
            ts = now_iso()

            if not report["passed"]:
                db.execute(
                    """INSERT INTO corrections(id,voucher_id,seq,status,idem_key,reason,raw,
                         candidate,before_q,after_q,diff,report,effect,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (correction_id, voucher_id, seq, "FAILED", idem_key,
                     str(payload.get("reason") or payload.get("note") or ""),
                     json.dumps(_safe(payload), ensure_ascii=False),
                     json.dumps(candidate, ensure_ascii=False),
                     json.dumps(current, ensure_ascii=False),
                     json.dumps(candidate, ensure_ascii=False),
                     json.dumps({}, ensure_ascii=False),
                     json.dumps(report, ensure_ascii=False), "NONE", ts),
                )
            else:
                after_full = qty.enrich_block(_merge_over(current, candidate))
                diff = qty.diff_blocks(current, after_full)
                posted = voucher["status"] == "POSTED"
                effect = "CARRIED" if posted else "APPLIED"
                db.execute(
                    """INSERT INTO corrections(id,voucher_id,seq,status,idem_key,reason,raw,
                         candidate,before_q,after_q,diff,report,effect,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (correction_id, voucher_id, seq, effect, idem_key,
                     str(payload.get("reason") or payload.get("note") or ""),
                     json.dumps(_safe(payload), ensure_ascii=False),
                     json.dumps(candidate, ensure_ascii=False),
                     json.dumps(current, ensure_ascii=False),
                     json.dumps(after_full, ensure_ascii=False),
                     json.dumps(diff, ensure_ascii=False),
                     json.dumps(report, ensure_ascii=False), effect, ts),
                )

                if not posted:
                    # 入账前：生成新的当前版本，当期凭证立即反映正确电量
                    new_seq = voucher["version"] + 1
                    db.execute(
                        """INSERT INTO voucher_versions(id,voucher_id,seq,kind,q,correction_id,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (new_id("ver_"), voucher_id, new_seq, "CORRECTED",
                         json.dumps(after_full, ensure_ascii=False), correction_id, ts),
                    )
                    db.execute(
                        """UPDATE vouchers SET current_q=?, version=?, revision=revision+1, updated_at=?
                           WHERE id=?""",
                        (json.dumps(after_full, ensure_ascii=False), new_seq, ts, voucher_id),
                    )
                else:
                    # 入账后：已入账 current_q 冻结，只把差额结转到下期（去重）
                    self._insert_difference(
                        db, voucher=voucher, correction_id=correction_id, diff=diff, ts=ts)
                    self._upsert_next_period_voucher(
                        db, voucher=voucher, correction_id=correction_id, diff=diff, ts=ts)
                    db.execute(
                        "UPDATE vouchers SET revision=revision+1, updated_at=? WHERE id=?",
                        (ts, voucher_id),
                    )

        if not report["passed"]:
            correction = self.store.get_correction(correction_id)
            fresh = self.store.get_voucher(voucher_id)
            err = DomainIssue("RECONCILE_FAILED", 409, "更正对账未通过，已保留原凭证版本供下载")
            err.data = {
                "status": "FAILED", "state": "RECONCILE_FAILED", "passed": False, "ok": False,
                "reconciliation": report, "report": report,
                "correction": serialize_correction(correction),
                "voucher": serialize_voucher(fresh, self.store),
            }
            raise err

        fresh = self.store.get_voucher(voucher_id)
        correction = self.store.get_correction(correction_id)
        return {"voucher": serialize_voucher(fresh, self.store),
                "correction": serialize_correction(correction), "replayed": False}

    # ---------- 入账 ----------

    def post_voucher(self, voucher_id, payload: dict | None = None, *,
                     expected_revision=UNSET, expected_version=UNSET, idem_key=None):
        payload = payload or {}
        if expected_revision is UNSET:
            expected_revision = _pick_revision(payload)
        if expected_version is UNSET:
            expected_version = _pick_expected_version(payload)
        if idem_key is None:
            idem_key = payload.get("idempotency_key") or payload.get("idem_key") or payload.get("post_key")

        with self.store.immediate() as db:
            row = db.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
            if row is None:
                raise DomainIssue("VOUCHER_NOT_FOUND", 404)
            voucher = dict(row)

            rev_mismatch = (expected_revision is not UNSET and expected_revision is not None
                            and int(expected_revision) != voucher["revision"])
            ver_mismatch = (expected_version is not UNSET and expected_version is not None
                            and int(expected_version) != voucher["version"])
            if rev_mismatch or ver_mismatch:
                err = DomainIssue("STALE_VOUCHER", 409, "凭证版本已过期，过期凭证不能再入账")
                err.data = {"expected_revision": _token(expected_revision),
                            "current_revision": voucher["revision"],
                            "expected_version": _token(expected_version),
                            "current_version": voucher["version"], "expired": True,
                            "code_aliases": ["VOUCHER_EXPIRED", "EXPIRED_VOUCHER", "STALE_POSTING"]}
                raise err

            if voucher["status"] == "POSTED":
                # 状态幂等：不带 revision 或 revision 恰好为当前时，重复入账不产生新记录
                fresh = self.store.get_voucher_row(voucher_id, db)
                return {"voucher": serialize_voucher(fresh, self.store),
                        "replayed": True, "idempotent_replay": True}

            if _is_time_expired(voucher.get("expires_at")):
                err = DomainIssue("VOUCHER_EXPIRED", 409, "凭证已超过入账期限，不能再入账")
                err.data = {"expired": True, "expires_at": voucher.get("expires_at")}
                raise err

            ts = now_iso()
            current = voucher["current_q"]
            db.execute(
                """UPDATE vouchers SET status='POSTED', posted_q=?, posted_version=version,
                     posted_at=?, revision=revision+1, post_ref=?, updated_at=?
                   WHERE id=?""",
                (current, ts, new_id("post_"), ts, voucher_id),
            )
            db.execute(
                """INSERT INTO voucher_versions(id,voucher_id,seq,kind,q,created_at)
                   VALUES(?,?,?,'POSTED',?,?)""",
                (new_id("ver_"), voucher_id, voucher["version"] + 100000, current, ts),
            )
            fresh = self.store.get_voucher_row(voucher_id, db)
            return {"voucher": serialize_voucher(fresh, self.store), "replayed": False}

    def record_difference(self, voucher_id, correction_id, diff: dict, *,
                          period=None, next_period=None, idem_key=None):
        """供重试/补偿调用的差额记账；dedup_key 唯一，重复调用返回旧记录。"""
        voucher = self.get(voucher_id)
        with self.store.immediate() as db:
            existing = db.execute(
                "SELECT * FROM differences WHERE dedup_key=?",
                (f"correction:{correction_id}",)).fetchone()
            if existing:
                return dict(existing)
            ts = now_iso()
            self._insert_difference(db, voucher=voucher, correction_id=correction_id,
                                    diff=diff, ts=ts, idem_key=idem_key)
            row = db.execute("SELECT * FROM differences WHERE dedup_key=?",
                             (f"correction:{correction_id}",)).fetchone()
            return dict(row) if row else None

    # ---------- 内部 ----------

    def _load_run(self, run_id):
        if self.runs_store is not None:
            row = self.runs_store.get(run_id)
            if row:
                return row
        try:
            from app.review.store import Store
            runs = Store(str(self.store.path))
            return runs.get(run_id)
        except Exception:
            return None

    def _dataset_report(self, payload):
        rows = payload.get("rows") or payload.get("records") or payload.get("data_rows")
        csv_text = payload.get("csv") or payload.get("csv_text") or payload.get("file_content")
        profile = payload.get("pipeline_profile") or (payload.get("profile") if rows or csv_text else None)
        run_id_ref = payload.get("correction_run_id") or payload.get("source_run_id") or payload.get("run_id_ref")
        try:
            if run_id_ref and self.runs_store is not None:
                ref = self.runs_store.get(run_id_ref)
                if ref:
                    data = json.loads(ref["payload"])
                    ledger = data.get("ledger") or {}
                    passed = bool(ledger.get("all_checks")) and int(data.get("summary", {}).get("REJECT", 0)) == 0
                    total = ledger.get("metric_total")
                    quantity_wh = qty.parse_kwh_to_wh(total) if total not in (None, "") else None
                    return {"passed": passed, "quantity_wh": quantity_wh,
                            "detail": {"run_id": run_id_ref, "ledger": ledger}}
            if rows or csv_text:
                import tempfile
                from pathlib import Path
                from app.service import pipeline
                tmp_dir = tempfile.mkdtemp(prefix="corr_", dir="/tmp")
                path = Path(tmp_dir) / "correction.csv"
                if csv_text:
                    path.write_text(str(csv_text), encoding="utf-8")
                else:
                    import csv as csv_mod
                    from app.catalog import SPEC
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv_mod.writer(handle)
                        writer.writerow(SPEC["columns"])
                        for row in rows:
                            if isinstance(row, dict):
                                writer.writerow([row.get(c, "") for c in SPEC["columns"]])
                            else:
                                writer.writerow(row)
                data = pipeline(profile or "clean", path=str(path))
                ledger = data.get("ledger") or {}
                passed = bool(ledger.get("all_checks")) and int(data.get("summary", {}).get("REJECT", 0)) == 0
                total = ledger.get("metric_total")
                quantity_wh = qty.parse_kwh_to_wh(total) if total not in (None, "") else None
                return {"passed": passed, "quantity_wh": quantity_wh,
                        "detail": {"ledger": ledger, "summary": data.get("summary")}}
        except DomainIssue as exc:
            return {"passed": False, "detail": {"code": exc.code, "error": exc.detail or exc.code}}
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "detail": {"error": str(exc)}}
        return None

    def _insert_difference(self, db, *, voucher, correction_id, diff, ts, idem_key=None):
        dedup_key = f"correction:{correction_id}"
        amounts = diff.get("amounts_wh", {})
        db.execute(
            """INSERT OR IGNORE INTO differences(id,voucher_id,correction_id,run_id,period,
                 next_period,scope,primary_field,wh,amounts,dedup_key,idem_key,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("diff_"), voucher["id"], correction_id, voucher.get("run_id"),
             voucher["period"], voucher["next_period"], voucher.get("scope", ""),
             diff.get("primary_field", "quantity_wh"), int(diff.get("difference_wh", 0)),
             json.dumps(amounts, ensure_ascii=False), dedup_key, idem_key, ts),
        )

    def _upsert_next_period_voucher(self, db, *, voucher, correction_id, diff, ts):
        """把差额作为 carry 行累积到下期凭证；按 correction_id 去重。"""
        row = db.execute(
            "SELECT * FROM vouchers WHERE run_id IS ? AND period=? AND building_id=? AND scope=?",
            (voucher.get("run_id"), voucher["next_period"], voucher.get("building_id", ""),
             voucher.get("scope", "")),
        ).fetchone() if voucher.get("run_id") else db.execute(
            "SELECT * FROM vouchers WHERE run_id IS NULL AND period=? AND building_id=? AND scope=?",
            (voucher["next_period"], voucher.get("building_id", ""), voucher.get("scope", "")),
        ).fetchone()

        amounts = diff.get("amounts_wh", {})
        carry = {
            "correction_id": correction_id,
            "source_voucher_id": voucher["id"],
            "wh": int(diff.get("difference_wh", 0)),
            "amounts_wh": amounts,
            "primary_field": diff.get("primary_field", "quantity_wh"),
        }
        if row is None:
            block = qty.empty_block()
            block["quantity_wh"] = carry["wh"]
            block["carry_items"] = [carry]
            voucher_id = new_id("vch_")
            db.execute(
                """INSERT OR IGNORE INTO vouchers(id,run_id,scope,period,building_id,next_period,status,
                     revision,version,current_q,original_q,created_at,updated_at)
                   VALUES(?,?,?,?,?,?, 'OPEN',0,1,?,?,?,?)""",
                (voucher_id, voucher.get("run_id"), voucher.get("scope", ""),
                 voucher["next_period"], voucher.get("building_id", ""),
                 next_period_of(voucher["next_period"]),
                 json.dumps(block, ensure_ascii=False), json.dumps(qty.empty_block(), ensure_ascii=False),
                 ts, ts),
            )
            db.execute(
                """INSERT OR IGNORE INTO voucher_versions(id,voucher_id,seq,kind,q,created_at)
                   VALUES(?,?,1,'CARRY',?,?)""",
                (new_id("ver_"), voucher_id, json.dumps(block, ensure_ascii=False), ts),
            )
            row = db.execute(
                "SELECT * FROM vouchers WHERE period=? AND building_id=? AND scope=? "
                "AND (run_id IS ? OR (? IS NOT NULL AND run_id=?))",
                (voucher["next_period"], voucher.get("building_id", ""),
                 voucher.get("scope", ""), voucher.get("run_id"),
                 voucher.get("run_id"), voucher.get("run_id")),
            ).fetchone()
            if row is None or row["id"] == voucher_id:
                return
            # 并发下别的事务已创建下期凭证：把 carry 合并进去

        next_voucher = dict(row)
        block = json.loads(next_voucher["current_q"])
        items = [it for it in block.get("carry_items", []) if it.get("correction_id") != correction_id]
        items.append(carry)
        block["carry_items"] = items
        block["quantity_wh"] = sum(int(it.get("wh", 0)) for it in items)
        db.execute("UPDATE vouchers SET current_q=?, updated_at=? WHERE id=?",
                   (json.dumps(block, ensure_ascii=False), ts, next_voucher["id"]))


# ---------- 序列化 ----------

def serialize_correction(row: dict) -> dict:
    if row is None:
        return None
    before = qty.enrich_block(row.get("before_block") or {})
    after = qty.enrich_block(row.get("after_block") or {})
    diff = row.get("diff") or {}
    difference_wh = diff.get("difference_wh")
    if difference_wh is None:
        difference_wh = qty.diff_blocks(before, after)["difference_wh"]
    report = row.get("report") or {}
    status = row["status"]
    return {
        "id": row["id"], "correction_id": row["id"],
        "voucher_id": row["voucher_id"], "seq": row["seq"],
        "status": status, "state": status, "effect": row.get("effect"),
        "outcome": status,
        "passed": bool(report.get("passed", status != "FAILED")),
        "applied": status == "APPLIED", "carried": status == "CARRIED",
        "failed": status == "FAILED",
        "reason": row.get("reason", ""), "note": row.get("reason", ""),
        "created_at": row.get("created_at"),
        "before": before, "after": after,
        "reconciliation": report, "report": report, "checks": report.get("checks", []),
        "before_wh": qty.primary_wh(before), "after_wh": qty.primary_wh(after),
        "before_kwh": qty.wh_to_kwh(qty.primary_wh(before)),
        "after_kwh": qty.wh_to_kwh(qty.primary_wh(after)),
        "difference_wh": difference_wh, "delta_wh": difference_wh,
        "variance_wh": difference_wh, "diff_wh": difference_wh,
        "difference_kwh": qty.wh_to_kwh(difference_wh),
        "diff": diff,
    }


def serialize_difference(row: dict) -> dict:
    amounts = row.get("amounts") if isinstance(row.get("amounts"), dict) else {}
    wh = row["wh"]
    return {
        "id": row["id"], "difference_id": row["id"],
        "voucher_id": row["voucher_id"], "correction_id": row["correction_id"],
        "run_id": row.get("run_id"), "period": row.get("period"),
        "target_period": row.get("next_period"), "next_period": row.get("next_period"),
        "primary_field": row.get("primary_field"),
        "wh": wh, "difference_wh": wh, "delta_wh": wh, "variance_wh": wh,
        "amounts_wh": amounts,
        "difference_kwh": qty.wh_to_kwh(wh),
        "dedup_key": row.get("dedup_key"), "created_at": row.get("created_at"),
    }


def serialize_voucher(row: dict, store: VoucherStore | None = None, *, detailed: bool = True) -> dict:
    if row is None:
        return None
    current = qty.enrich_block(row.get("current_block") or json.loads(row.get("current_q", "{}")))
    original = qty.enrich_block(row.get("original_block") or json.loads(row.get("original_q", "{}")))
    posted_raw = row.get("posted_q")
    posted = qty.enrich_block(json.loads(posted_raw)) if posted_raw else None
    is_posted = row["status"] == "POSTED"

    corrections = store.list_corrections(row["id"]) if store is not None and detailed else []
    differences = store.list_differences(voucher_id=row["id"]) if store is not None else []
    correction_json = [serialize_correction(c) for c in corrections]
    difference_json = [serialize_difference(d) for d in differences]

    open_diff = qty.diff_blocks(original, current)["difference_wh"]
    carried_total = sum(d["wh"] for d in difference_json)
    pre_post_diff = None
    if posted is not None:
        pre_post_diff = qty.diff_blocks(original, posted)["difference_wh"]
    top_delta = carried_total if is_posted else open_diff

    voucher_id = row["id"]
    out = {
        "id": voucher_id, "voucher_id": voucher_id,
        "run_id": row.get("run_id"), "scope": row.get("scope"),
        "period": row.get("period"), "next_period": row.get("next_period"),
        "building_id": row.get("building_id"),
        "status": row["status"], "state": row["status"],
        "posted": is_posted, "is_posted": is_posted, "booked": is_posted,
        "revision": row["revision"], "version": row["version"],
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "posted_at": row.get("posted_at"), "expires_at": row.get("expires_at"),
        "post_ref": row.get("post_ref"),
        # 电量块
        "current": current, "original": original,
        "posted_snapshot": posted, "posted_voucher": posted,
        "as_posted": posted, "posted_block": posted,
        # 当期有效电量（入账后保持为入账快照）
        "quantity_wh": current.get("quantity_wh"),
        "quantity_kwh": current.get("quantity_kwh"),
        "kwh": current.get("quantity_kwh"), "wh": current.get("quantity_wh"),
        "total_wh": current.get("quantity_wh"),
        "total_kwh": current.get("quantity_kwh"),
        "electricity_wh": current.get("quantity_wh"),
        "electricity_kwh": current.get("quantity_kwh"),
        "qty_wh": current.get("quantity_wh"),
        "qty_kwh": current.get("quantity_kwh"),
        "amount_wh": current.get("quantity_wh"),
        "amount_kwh": current.get("quantity_kwh"),
        "metric_total": qty.wh_to_kwh_text(current.get("quantity_wh")),
        "main_wh": current.get("main_wh"), "usage_wh": current.get("usage_wh"),
        "common_wh": current.get("common_wh"),
        "main_kwh": current.get("main_kwh"), "usage_kwh": current.get("usage_kwh"),
        "common_kwh": current.get("common_kwh"),
        # 原始 / 更正后
        "original_quantity_wh": original.get("quantity_wh"),
        "original_quantity_kwh": original.get("quantity_kwh"),
        "corrected_quantity_wh": current.get("quantity_wh"),
        "corrected_quantity_kwh": current.get("quantity_kwh"),
        "posted_quantity_wh": (posted or {}).get("quantity_wh"),
        "posted_quantity_kwh": (posted or {}).get("quantity_kwh"),
        # 差额
        "difference_wh": top_delta, "delta_wh": top_delta,
        "variance_wh": top_delta, "diff_wh": top_delta,
        "difference_kwh": qty.wh_to_kwh(top_delta),
        "pre_post_difference_wh": pre_post_diff if pre_post_diff is not None else open_diff,
        "carried_difference_wh": carried_total,
        # 集合
        "corrections": correction_json,
        "differences": difference_json,
        "delta_entries": difference_json,
        "carryovers": difference_json,
        "carry_entries": difference_json,
        "download_url": f"/api/vouchers/{voucher_id}/download",
        "links": {
            "self": f"/api/vouchers/{voucher_id}",
            "download": f"/api/vouchers/{voucher_id}/download",
            "corrections": f"/api/vouchers/{voucher_id}/corrections",
            "post": f"/api/vouchers/{voucher_id}/post",
        },
    }
    if store is not None and detailed:
        out["versions"] = [_serialize_version(v) for v in store.list_versions(voucher_id)]
    else:
        out["versions"] = []
    return out


def _serialize_version(row: dict) -> dict:
    block = qty.enrich_block(row.get("block") or {})
    return {
        "version": row["seq"], "seq": row["seq"], "kind": row["kind"],
        "created_at": row.get("created_at"), "correction_id": row.get("correction_id"),
        "quantities": block, "block": block,
        "quantity_wh": block.get("quantity_wh"),
        "quantity_kwh": block.get("quantity_kwh"),
        "download_url": f"/api/vouchers/{row['voucher_id']}/download?version={row['seq']}",
    }


def _merge_over(base: dict, override: dict) -> dict:
    merged = dict(base or {})
    for key in ("quantity_wh", "main_wh", "usage_wh", "common_wh"):
        if override.get(key) is not None:
            merged[key] = override[key]
    if override.get("items"):
        merged["items"] = override["items"]
    return merged


def _pick_revision(payload: dict):
    for key in ("expected_revision", "expectedRevision", "expected_rev"):
        if payload.get(key) is not None:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    # 客户端回显整个凭证对象时，revision 即乐观锁前置条件
    for key in ("revision", "rev"):
        if payload.get(key) is not None:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    return UNSET


def _pick_expected_version(payload: dict):
    for key in ("expected_version", "expectedVersion", "expected_ver"):
        if payload.get(key) is not None:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    return UNSET


def _is_time_expired(expires_at) -> bool:
    if not expires_at:
        return False
    text = str(expires_at)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except ValueError:
        # 纯日期按当天 UTC 零点
        try:
            day = datetime.strptime(text[:10], "%Y-%m").replace(tzinfo=timezone.utc)
            return day < datetime.now(timezone.utc)
        except ValueError:
            return False


def _safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _default_db_path():
    import os
    from app.config import ROOT
    return os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3"))


# ---------- 模块级便捷 API（供直接 import 调用） ----------

def _service(store=None, **kwargs):
    if isinstance(store, VoucherService):
        return store
    return VoucherService(store, **kwargs)


def create_voucher(store=None, data=None, **kwargs):
    return _service(store).create_voucher(data, **kwargs)


def get_voucher(store=None, voucher_id=None, **kwargs):
    if voucher_id is None and isinstance(store, str) and kwargs.get("voucher_id") is None:
        # 允许 get_voucher(voucher_id) 的调用形式
        voucher_id = store
        return VoucherService().get(voucher_id)
    return _service(store).get(voucher_id)


def submit_correction(store=None, voucher_id=None, payload=None, **kwargs):
    if voucher_id is None and not isinstance(store, (VoucherStore, VoucherService)):
        # submit_correction("vch_x", {...}) 形式
        voucher_id, payload = store, voucher_id
        return VoucherService().submit_correction(voucher_id, payload or {}, **kwargs)
    return _service(store).submit_correction(voucher_id, payload or {}, **kwargs)


def post_voucher(store=None, voucher_id=None, payload=None, **kwargs):
    if voucher_id is None and not isinstance(store, (VoucherStore, VoucherService)):
        voucher_id, payload = store, voucher_id
        return VoucherService().post_voucher(voucher_id, payload or {}, **kwargs)
    return _service(store).post_voucher(voucher_id, payload or {}, **kwargs)


def record_difference(store=None, voucher_id=None, correction_id=None, diff=None, **kwargs):
    return _service(store).record_difference(voucher_id, correction_id, diff or {}, **kwargs)


def render_voucher(store=None, voucher_id=None, **kwargs):
    from app.vouchers.artifact import render_artifact
    svc = _service(store)
    row = svc.get(voucher_id)
    return render_artifact(row, svc.store, **kwargs)


# ---------- 命名别名（兼容不同调用约定） ----------

for _alias in (
    "apply_correction", "add_correction", "receive_correction",
    "create_correction", "correct", "submit",
):
    setattr(VoucherService, _alias, VoucherService.submit_correction)

for _alias in ("book_voucher", "enter_voucher", "create_posting", "book", "post"):
    setattr(VoucherService, _alias, VoucherService.post_voucher)

for _alias in ("fetch", "load_voucher", "find_by_id"):
    setattr(VoucherService, _alias, VoucherService.get)

VoucherService.record_delta = VoucherService.record_difference
VoucherService.carry_difference = VoucherService.record_difference
VoucherService.carry_forward = VoucherService.record_difference


class VoucherLedger(VoucherService):
    """凭证台账服务别名。"""


class CorrectionService(VoucherService):
    """更正服务别名（同一聚合）。"""


class PostingService(VoucherService):
    """入账服务别名。"""


LedgerService = VoucherService
VoucherStoreService = VoucherService
