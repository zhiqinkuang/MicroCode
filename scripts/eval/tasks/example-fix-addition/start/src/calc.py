"""演示夹具的起始实现：故意写错，等着被测 agent 修。"""


def add(a, b):
    # BUG：应当是 a + b
    return a - b


def multiply(a, b):
    return a * b
