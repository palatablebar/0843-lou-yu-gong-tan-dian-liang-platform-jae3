"""兼容 app.corrections 命名。"""
from app.vouchers.service import (  # noqa: F401
    VoucherService,
    submit_correction,
    serialize_correction,
)
