"""兼容 from app.correction.service import ..."""
from app.vouchers.service import *  # noqa: F401,F403
from app.vouchers.service import (  # noqa: F401
    VoucherService, submit_correction, serialize_correction,
)
