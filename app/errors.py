from flask import jsonify
import json as _json


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "VOUCHER_NOT_FOUND": "凭证不存在",
    "CORRECTION_NOT_FOUND": "更正记录不存在",
    "STALE_VOUCHER": "凭证版本已过期，过期凭证不能再入账",
    "VOUCHER_STALE": "凭证版本已过期，过期凭证不能再入账",
    "EXPIRED_VOUCHER": "凭证已超过入账期限，不能再入账",
    "VOUCHER_EXPIRED": "凭证已超过入账期限，不能再入账",
    "RECONCILE_FAILED": "更正对账未通过，已保留原凭证版本供下载",
    "CORRECTION_RECONCILE_FAILED": "更正对账未通过，已保留原凭证版本供下载",
    "ALREADY_POSTED": "凭证已入账，不能重复入账",
    "VOUCHER_POSTED": "凭证已入账",
    "IDEMPOTENCY_CONFLICT": "相同幂等键对应不同请求内容",
    "NOT_FOUND": "接口或资源不存在",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        self.data = None
        super().__init__(code)


def error_response(exc: DomainIssue):
    body = {"error": exc.detail or MESSAGES.get(exc.code, "请求无法处理"), "code": exc.code}
    if getattr(exc, "data", None):
        body.update(exc.data)
    # 保证错误体一定可 JSON 序列化，避免构造错误响应时二次异常
    safe = _json.loads(_json.dumps(body, ensure_ascii=False, default=str))
    return jsonify(safe), exc.status
