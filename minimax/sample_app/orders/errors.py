# 作者：FDE WorkMate
"""订单查询错误类型。"""


class InvalidQueryError(ValueError):
    """查询参数不合法时抛出，message 必须指出出错的参数（REQ-ORD-002）。"""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(f"{parameter}: {message}")
        self.parameter = parameter
