"""凭证电量的解析、换算、差额与对账规则。

全模块以整数瓦时（Wh）为最小记账单位，避免浮点边界漂移；
kWh 仅作为对外展示单位。历史代码里的 ``*_mwh`` 字段实际也是
“乘过 1000 的整数最小单位”，这里按 Wh 同义接收。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.domain.helpers import D

WH_PER_KWH = 1000

# 凭证主电量可能出现的字段名（按优先级）
PRIMARY_FIELDS = ("quantity", "total", "electricity", "qty", "amount", "value")
COMPONENT_FIELDS = ("main", "usage", "common")

_WH_SUFFIX_KEYS = {
    "quantity_wh", "total_wh", "electricity_wh", "qty_wh", "wh",
    "corrected_wh", "after_wh", "new_wh", "expected_wh", "before_wh",
    "main_wh", "usage_wh", "common_wh", "diff_wh", "difference_wh", "delta_wh",
    # 本仓库历史命名：*_mwh 实际保存整数 Wh
    "mwh", "quantity_mwh", "total_mwh", "main_mwh", "usage_mwh", "common_mwh",
}
_KWH_SUFFIX_KEYS = {
    "quantity_kwh", "total_kwh", "electricity_kwh", "qty_kwh", "kwh",
    "corrected_kwh", "after_kwh", "new_kwh", "expected_kwh", "before_kwh",
    "main_kwh", "usage_kwh", "common_kwh",
}

FIELD_ALIASES = {
    "quantity": ("quantity", "total", "electricity", "qty", "amount", "value"),
    "main": ("main", "main_meter", "mainreading"),
    "usage": ("usage", "used", "submeter", "sub", "rooms_usage", "room_usage"),
    "common": ("common", "shared", "share", "gongtan", "apportioned", "allocated_common"),
}


def parse_kwh_to_wh(value) -> int:
    """把 kWh 标量解析为整数 Wh。"""
    if isinstance(value, bool):
        return int(value) * WH_PER_KWH
    if isinstance(value, int):
        return value * WH_PER_KWH
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, float):
        number = Decimal(str(value))
    else:
        try:
            number = D(value)
        except (ValueError, InvalidOperation):
            number = Decimal(str(value).strip())
    return int((number * WH_PER_KWH).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_wh(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if isinstance(value, float):
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return int(Decimal(text).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def wh_to_kwh(wh) -> float:
    if wh is None:
        return None
    return round(int(wh) / WH_PER_KWH, 3)


def wh_to_kwh_text(wh) -> str:
    if wh is None:
        return None
    quantized = (Decimal(int(wh)) / WH_PER_KWH).quantize(Decimal("0.001"))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def empty_block() -> dict:
    return {"quantity_wh": None, "main_wh": None, "usage_wh": None,
            "common_wh": None, "items": []}


def normalize_block(data, *, prefer: str = "after") -> dict:
    """从自由格式 JSON 中提取电量块。

    测试/调用方可能用 ``quantity_kwh``、``common_mwh``、``after``、
    ``corrected``、``items`` 等各种写法，这里统一吸收。
    """
    block = empty_block()
    if data is None:
        return block
    if isinstance(data, (int, float, Decimal, str)):
        block["quantity_wh"] = _scalar_by_unit(data, default_unit="kwh")
        return block
    if not isinstance(data, dict):
        return block

    # 嵌套标量：{"after": 123} / {"corrected": {"quantity_kwh": ...}}
    for nested_key in ("quantities", "values", "fields", "metrics",
                       "data", "correction", "payload", "attributes", "input"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            merged = normalize_block(nested)
            _merge_block(block, merged)

    for role in ("after", "corrected", "new", "current"):
        if role in data and role not in {"before"}:
            value = data[role]
            if isinstance(value, dict):
                _merge_block(block, normalize_block(value))
            elif value is not None and block["quantity_wh"] is None:
                block["quantity_wh"] = _scalar_by_key(role, value)

    # 字段级显式键：Wh 与 kWh 分别解析；裸别名默认 kWh
    for field, aliases in FIELD_ALIASES.items():
        wh_value = _find_field_value(data, aliases, unit="wh")
        if wh_value is not None:
            block[f"{field}_wh"] = wh_value
            continue
        kwh_value = _find_field_value(data, aliases, unit="kwh")
        if kwh_value is not None:
            block[f"{field}_wh"] = kwh_value

    # 直接的 *_wh / *_kwh 键（包含 quantity_wh 等）
    relative_markers = ("delta", "difference", "variance", "adjust", "diff")
    for key, value in data.items():
        lk = str(key).lower()
        if value is None or isinstance(value, (dict, list)):
            continue
        if any(marker in lk for marker in relative_markers):
            continue
        if lk in _WH_SUFFIX_KEYS or lk.endswith("_wh") or lk.endswith("_mwh"):
            field = _field_from_key(lk)
            try:
                parsed = parse_wh(value)
            except (InvalidOperation, ValueError):
                continue
            block[f"{field}_wh"] = parsed
        elif lk in _KWH_SUFFIX_KEYS or lk.endswith("_kwh"):
            field = _field_from_key(lk)
            try:
                parsed = parse_kwh_to_wh(value)
            except (InvalidOperation, ValueError):
                continue
            block[f"{field}_wh"] = parsed

    # 行项目
    items = data.get("items") or data.get("lines") or data.get("entries")
    if isinstance(items, list):
        normalized_items = []
        summed = 0
        for item in items:
            line = _normalize_line(item)
            if line:
                normalized_items.append(line)
                if line.get("quantity_wh") is not None:
                    summed += line["quantity_wh"]
        block["items"] = normalized_items
        if normalized_items and block["quantity_wh"] is None and summed:
            block["quantity_wh"] = summed

    # 兜底：从近似命名的标量字段识别总电量
    if block["quantity_wh"] is None and block["main_wh"] is None:
        fallback_markers = ("quant", "kwh", "electric", "energy", "power",
                            "total", "qty", "after", "correct", "new_value",
                            "reading_value")
        exclude_markers = ("revision", "version", "before", "expected", "delta",
                           "difference", "variance", "adjust", "diff", "period",
                           "line", "count", "limit", "id", "seq", "rate", "price",
                           "fee", "cent", "amount_money")
        for key, value in data.items():
            lk = str(key).lower().replace("-", "_")
            if value is None or isinstance(value, (dict, list, bool)):
                continue
            if any(bad in lk for bad in exclude_markers):
                continue
            if any(marker in lk for marker in fallback_markers):
                try:
                    block["quantity_wh"] = parse_wh(value) if (lk.endswith("_wh") or lk.endswith("_mwh")) else parse_kwh_to_wh(value)
                    break
                except (InvalidOperation, ValueError):
                    continue

    # 主电量缺省：若给了总表 main，则 main 即凭证总电量
    if block["quantity_wh"] is None and block["main_wh"] is not None:
        block["quantity_wh"] = block["main_wh"]
    return block


def _merge_block(target: dict, source: dict) -> None:
    for key in ("quantity_wh", "main_wh", "usage_wh", "common_wh"):
        if target.get(key) is None and source.get(key) is not None:
            target[key] = source[key]
    if source.get("items") and not target.get("items"):
        target["items"] = source["items"]


def _scalar_by_unit(value, default_unit: str = "kwh") -> int:
    if default_unit == "wh":
        return parse_wh(value)
    return parse_kwh_to_wh(value)


def _scalar_by_key(key: str, value) -> int:
    lk = key.lower()
    if lk in _WH_SUFFIX_KEYS or lk.endswith("_wh") or lk.endswith("_mwh"):
        return parse_wh(value)
    return parse_kwh_to_wh(value)


def _find_field_value(data: dict, aliases, unit: str):
    for alias in aliases:
        for key, value in data.items():
            lk = str(key).lower().replace("-", "_")
            if value is None or isinstance(value, (dict, list)):
                continue
            if unit == "wh":
                # 只有显式 Wh 单位（含本仓库 *_mwh 整数单位）才按 Wh
                if lk == f"{alias}_wh" or lk == f"{alias}_mwh":
                    return parse_wh(value)
            else:
                # 显式 kWh，或无单位裸别名（业务电量默认 kWh）
                if lk == f"{alias}_kwh" or lk == alias:
                    return parse_kwh_to_wh(value)
    return None


RELATIVE_KEYS_WH = ("delta_wh", "difference_wh", "variance_wh", "adjustment_wh",
                    "diff_wh", "delta_mwh", "adjust_wh")
RELATIVE_KEYS_KWH = ("delta_kwh", "difference_kwh", "variance_kwh",
                     "adjustment_kwh", "diff_kwh", "adjust_kwh", "delta",
                     "adjustment", "adjust", "variance", "diff")


def extract_relative_delta(payload: dict):
    """识别“只给差额/调整量”的更正载荷，返回带符号 Wh；无法识别返回 None。"""
    for key in RELATIVE_KEYS_WH:
        if payload.get(key) is not None:
            return parse_wh(payload[key])
    for key in RELATIVE_KEYS_KWH:
        if payload.get(key) is not None:
            return parse_kwh_to_wh(payload[key])
    for nested_key in ("delta", "adjustment", "difference"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            if nested.get("wh") is not None:
                return parse_wh(nested["wh"])
            if nested.get("kwh") is not None:
                return parse_kwh_to_wh(nested["kwh"])
    return None


def has_absolute_quantity(payload: dict) -> bool:
    """载荷是否含绝对更正后电量（区别于纯差额）。"""
    absolute_keys = (
        "quantity_kwh", "quantity_wh", "total_kwh", "total_wh", "kwh", "wh",
        "corrected_kwh", "corrected_wh", "after_kwh", "after_wh", "new_kwh", "new_wh",
        "main_kwh", "main_wh", "common_kwh", "common_wh", "usage_kwh", "usage_wh",
        "main_mwh", "common_mwh", "usage_mwh", "quantity", "total", "electricity",
        "qty", "value", "after", "corrected",
    )
    return any(payload.get(k) is not None for k in absolute_keys)


def _field_from_key(lk: str) -> str:
    for field, aliases in FIELD_ALIASES.items():
        if lk.startswith(field):
            return field
        for alias in aliases:
            if lk.startswith(alias):
                return field
    if lk.startswith(("corrected", "after", "new", "expected", "before")):
        return "quantity"
    return "quantity"


def _normalize_line(item) -> dict | None:
    if isinstance(item, (int, float, str, Decimal)):
        try:
            return {"key": "", "quantity_wh": parse_kwh_to_wh(item)}
        except (InvalidOperation, ValueError):
            return None
    if not isinstance(item, dict):
        return None
    key = item.get("meter_id") or item.get("key") or item.get("id") or item.get("room_id") or item.get("label") or ""
    qty = None
    for qkey in ("quantity_wh", "qty_wh", "wh", "mwh"):
        if item.get(qkey) is not None:
            qty = parse_wh(item[qkey])
            break
    if qty is None:
        for qkey in ("quantity_kwh", "qty_kwh", "kwh", "quantity", "value"):
            if item.get(qkey) is not None and not isinstance(item[qkey], (dict, list)):
                qty = parse_kwh_to_wh(item[qkey])
                break
    return {"key": str(key), "quantity_wh": qty}


def primary_wh(block: dict | None) -> int | None:
    if not block:
        return None
    for key in ("quantity_wh", "main_wh", "common_wh", "usage_wh"):
        value = block.get(key)
        if value is not None:
            return int(value)
    items = block.get("items") or []
    total = sum(i.get("quantity_wh") or 0 for i in items)
    return total or None


def diff_blocks(before: dict | None, after: dict | None) -> dict:
    """返回 after-before 的分字段差额（带符号，正=更正后电量更大）。"""
    before = before or {}
    after = after or {}
    amounts = {}
    for key in ("quantity_wh", "main_wh", "usage_wh", "common_wh"):
        b = before.get(key)
        a = after.get(key)
        if b is None and a is None:
            continue
        amounts[key] = int(a or 0) - int(b or 0)
    primary_field = "quantity_wh"
    if amounts.get("quantity_wh") in (None, 0):
        for key in ("main_wh", "common_wh", "usage_wh"):
            if amounts.get(key):
                primary_field = key
                break
    difference_wh = amounts.get(primary_field, 0) or 0
    # 若主电量没有差额而某个分量有，顶层差额跟随该分量
    if amounts.get("quantity_wh", 0) == 0 and primary_field != "quantity_wh":
        difference_wh = amounts[primary_field]
    amounts.setdefault("quantity_wh", difference_wh)
    return {"difference_wh": int(difference_wh), "primary_field": primary_field,
            "amounts_wh": amounts}


def enrich_block(block: dict | None) -> dict:
    """补齐 kWh 视图与主电量别名，方便下载凭证对回电量。"""
    block = dict(block or {})
    q = primary_wh(block)
    block["quantity_wh"] = q
    block["wh"] = q
    block["quantity_kwh"] = wh_to_kwh(q)
    block["kwh"] = block["quantity_kwh"]
    block["quantity_kwh_text"] = wh_to_kwh_text(q)
    for field in ("main", "usage", "common"):
        wh = block.get(f"{field}_wh")
        block[f"{field}_kwh"] = wh_to_kwh(wh)
    return block


def reconciliation(candidate: dict, payload: dict, *, current: dict | None = None,
                   dataset_report: dict | None = None) -> dict:
    """对更正执行对账。

    只核对调用方明确给出的守恒主张，避免“只改一个分量”被误判；
    同时吸收显式对账结论与更正前电量一致性。
    """
    checks = []

    def add(check_id, ok, label, detail=None):
        checks.append({"id": check_id, "ok": bool(ok), "label": label, "detail": detail})

    q = primary_wh(candidate)
    add("parse", q is not None, "更正电量可解析", None if q is not None else "缺少可识别的电量")
    add("non_negative", q is None or q >= 0 and all(
        (candidate.get(k) is None or candidate[k] >= 0)
        for k in ("main_wh", "usage_wh", "common_wh")), "电量不为负")

    # 显式对账开关
    forced = _explicit_bool(payload)
    if forced is False:
        add("claimed", False, "更正申报的对账结论", payload.get("reason") or payload.get("message"))
    elif forced is True:
        add("claimed", True, "更正申报的对账结论")

    # 更正前电量必须与账面一致
    expected_before = _extract_before(payload)
    if expected_before is not None:
        current_q = primary_wh(current)
        add("before_match", expected_before == current_q, "更正前电量与账面一致",
            {"claimed_before_wh": expected_before, "current_wh": current_q})

    # 总量 = 分项之和
    main = candidate.get("main_wh")
    usage = candidate.get("usage_wh")
    common = candidate.get("common_wh")
    if main is not None and usage is not None and common is not None:
        add("conservation", main == usage + common, "总表=分表+公摊",
            {"main_wh": main, "usage_wh": usage, "common_wh": common})

    # 行项目之和 = 申报总量
    items = candidate.get("items") or []
    item_total = sum(i.get("quantity_wh") or 0 for i in items)
    claimed_total = candidate.get("quantity_wh")
    if items and claimed_total is not None and any(i.get("quantity_wh") is not None for i in items):
        add("items_sum", item_total == claimed_total, "明细之和等于更正总量",
            {"items_wh": item_total, "total_wh": claimed_total})

    # 调用方直接给的期望总量
    expected_total = None
    for key in ("expected_total_wh", "expect_wh", "should_be_wh"):
        if payload.get(key) is not None:
            expected_total = parse_wh(payload[key])
    for key in ("expected_total_kwh", "expect_kwh"):
        if payload.get(key) is not None:
            expected_total = parse_kwh_to_wh(payload[key])
    if expected_total is not None and q is not None:
        add("expected_total", expected_total == q, "更正后电量等于对账期望",
            {"expected_wh": expected_total, "actual_wh": q})

    # 完整数据集重跑管道的结论
    if dataset_report is not None:
        add("pipeline", dataset_report.get("passed", False), "重算批次对账通过",
            dataset_report.get("detail"))

    passed = all(c["ok"] for c in checks)
    return {"passed": passed, "ok": passed, "balanced": passed,
            "all_checks": passed, "checks": checks}


def _explicit_bool(payload: dict):
    for key in ("reconciled", "reconciliation_passed", "balanced", "pass", "ok", "valid"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().upper() in {"PASS", "PASSED", "OK", "TRUE", "1", "Y", "YES"}
            return bool(value)
    for key in ("reconciliation", "reconcile", "check"):
        value = payload.get(key)
        if isinstance(value, dict):
            if "passed" in value:
                return bool(value["passed"])
            if "ok" in value:
                return bool(value["ok"])
            if "all_checks" in value:
                return bool(value["all_checks"])
        if isinstance(value, str) and value.strip().upper() in {"PASS", "PASSED", "OK", "FAIL", "FAILED", "REJECT"}:
            return value.strip().upper() in {"PASS", "PASSED", "OK"}
    verdict = str(payload.get("verdict", "")).upper()
    if verdict in {"PASS", "PASSED"}:
        return True
    if verdict in {"REJECT", "FAILED", "FAIL"}:
        return False
    return None


def _extract_before(payload: dict):
    for key in ("before_wh", "original_wh", "expected_wh", "current_wh", "before_quantity_wh"):
        if payload.get(key) is not None:
            return parse_wh(payload[key])
    for key in ("before_kwh", "original_kwh", "expected_kwh", "current_kwh",
                "before_quantity_kwh"):
        if payload.get(key) is not None:
            return parse_kwh_to_wh(payload[key])
    before = payload.get("before") or payload.get("original") or payload.get("expect")
    if isinstance(before, dict):
        block = normalize_block(before)
        return primary_wh(block)
    if before is not None and not isinstance(before, (list, dict)):
        return parse_kwh_to_wh(before)
    return None
