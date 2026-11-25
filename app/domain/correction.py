"""命名兼容垫片：实现见 app.vouchers。"""
from app.vouchers.service import (  # noqa: F401
    VoucherService, submit_correction as apply_correction, submit_correction,
)
