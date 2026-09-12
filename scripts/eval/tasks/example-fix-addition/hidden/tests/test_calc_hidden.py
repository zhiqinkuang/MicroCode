"""演示夹具的隐藏用例：模型看不到，用来抓「只对可见用例硬编码」。"""
from src.calc import add


def test_add_zero():
    assert add(0, 7) == 7


def test_add_large():
    assert add(10**6, 1) == 10**6 + 1


def test_add_floats():
    assert add(0.5, 0.25) == 0.75
