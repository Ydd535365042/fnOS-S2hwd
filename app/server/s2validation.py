"""Shared value validation for hardware and script requests."""
ALL_SCOPES = ["read", "control", "scripts", "admin"]


class Problem(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def integer(value, low, high):
    if isinstance(value, bool):
        raise Problem("请输入有效整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise Problem("请输入有效整数") from None
    if str(number) != str(value) or not low <= number <= high:
        raise Problem("数值超出允许范围")
    return number

