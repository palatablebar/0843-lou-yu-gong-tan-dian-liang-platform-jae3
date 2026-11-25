"""兼容单数命名 app.voucher，全部实现位于 app.vouchers。"""
from app.vouchers import *  # noqa: F401,F403
from app.vouchers import service, store, quantities, artifact  # noqa: F401
from app.vouchers.service import (  # noqa: F401
    VoucherService,
    serialize_voucher,
    create_voucher,
    get_voucher,
    submit_correction,
    post_voucher,
    render_voucher,
)
from app.vouchers.store import VoucherStore  # noqa: F401
