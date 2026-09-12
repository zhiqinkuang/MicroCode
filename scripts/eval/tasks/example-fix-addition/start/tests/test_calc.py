"""演示夹具的可见测试：模型看得到这三个用例。"""
from src.calc import add, multiply


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, -1) == -2


def test_multiply_unrelated():
    # 与本次修复无关，确保 agent 不会把整个文件搞坏
    assert multiply(3, 4) == 12
