"""命名兼容垫片：实现见 app.vouchers。"""
from app.vouchers.service import (  # noqa: F401
    VoucherService, create_voucher, get_voucher, submit_correction, post_voucher,
    record_difference, render_voucher, serialize_voucher,
)
from app.vouchers.quantities import diff_blocks, parse_kwh_to_wh  # noqa: F401
