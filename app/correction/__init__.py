"""兼容 app.correction 命名。"""
from app.vouchers.service import (  # noqa: F401
    VoucherService,
    submit_correction as apply,
    submit_correction,
)


def reconcile(*args, **kwargs):
    """对账并提交更正的便捷入口。"""
    return VoucherService(kwargs.pop("store", None)).submit_correction(*args, **kwargs)
