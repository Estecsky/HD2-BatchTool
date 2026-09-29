"""切口法向误差策略；有限性与数据完整性不受可调阈值影响。"""

import math

DEFAULT_WARNING = 1.0e-3
DEFAULT_LIMIT = 5.0e-3


def validate_thresholds(warning, limit):
    warning, limit = float(warning), float(limit)
    if not all(math.isfinite(v) for v in (warning, limit)):
        raise ValueError("法向阈值必须是有限数值")
    # RNA FloatProperty is float32; its inclusive 1e-6 minimum round-trips
    # slightly below the Python literal. Normalize only that encoding error.
    warning = 1.e-6 if math.isclose(warning, 1.e-6, rel_tol=1.e-7) else warning
    limit = 1.e-6 if math.isclose(limit, 1.e-6, rel_tol=1.e-7) else limit
    if not 1.0e-6 <= warning <= limit <= 2.0:
        raise ValueError("法向阈值须满足 0.000001 ≤ 警告值 ≤ 阻断值 ≤ 2")
    return warning, limit


def classify_error(error, warning=DEFAULT_WARNING, limit=DEFAULT_LIMIT):
    warning, limit = validate_thresholds(warning, limit)
    error = float(error)
    if not math.isfinite(error) or error < 0:
        raise ValueError("法向误差无效；调整阈值不能放行非有限数据")
    return "BLOCK" if error > limit else "WARNING" if error > warning else "OK"
