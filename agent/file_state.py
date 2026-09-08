"""
readFileState：read_file / edit_file / write_file 共享的会话级状态。
每个会话各持有一个实例，作为依赖注入给工具；用 /new、/resume 切换会话时换上一个全新的。
它为每个文件记录「读过没有、读到的是什么、读取那一刻的 mtime」。
"""

import os


class ReadFileState:
    """
    持有一个会话的读取状态。edit_file 和 write_file 改文件前都查它：
    没读过不让改，读完又被别人改过也不让改。
    """
    def __init__(self):
        # 绝对路径 -> {"content": 读到的内容, "timestamp": 读取那一刻的 mtime, "offset"/"limit": 读取范围}
        self._state: dict = {}

    def record(self, path: str, content: str, offset: int | None = None, limit: int | None = None) -> None:
        """
        登记一个文件：存下刚读到的内容、此刻文件的 mtime、以及读取范围（offset/limit）。
        read_file 会传入非 None 的 offset；edit_file / write_file 不传（保持 None），标记这是一条全量登记、去重时跳过，借此区分两种来源。
        """
        self._state[os.path.abspath(path)] = {
            "content": content,
            "timestamp": os.path.getmtime(path),
            "offset": offset,
            "limit": limit,
        }

    # 检查是否已经进行了手动修改
    def stale_paths(self):
        stale = []
        for path, record in self._state.items():
            try:
                if os.path.getmtime(path) > record["timestamp"]:
                    stale.append(path)
            except OSError:
                continue
        return stale

    def get(self, path: str):
        """
        取出一个文件的登记，从没读过就返回 None。
        """
        return self._state.get(os.path.abspath(path))
    def paths(self) -> list[str]:
        """
        所有登记过的绝对路径，按首次读取顺序排列。
        """
        return list(self._state)
    def invalidate(self, path: str) -> None:
        """
        丢掉一个文件的登记：/rewind 把文件恢复成旧版本后调用，强制下次 edit 前重新 read_file。
        rewind_files 用 copy2 带回备份的旧 mtime，可能让 edit_file 的 mtime 检查漏掉变化，
        清掉登记最稳妥——没登记就会被「先读后写」约束打回重读。
        """
        self._state.pop(os.path.abspath(path), None)
