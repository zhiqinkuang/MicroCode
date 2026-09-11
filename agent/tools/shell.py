"""
shell 工具：run_command，命中高危特征时通过自检强制走审批。
"""
import re
import subprocess

import permissions
def run_command(command: str) -> str:
    """
    执行一条 shell 命令并返回输出。
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, errors="replace", timeout=120
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\n[错误] {result.stderr}"
        return output or "(无输出)"
    except subprocess.TimeoutExpired:
        return "[错误] 命令执行超时（120秒）"




# 高危命令的特征：删除文件、提权、直写磁盘
# rm 用 lookbehind 排除 git rm / npm rm 这类包管理器子命令（它们前面会带 "git "/"npm "）
# find / 或 find ~ 是全盘/家目录搜索：在 agent 场景下几乎总是 LLM 在文件不存在时走偏，
# 命令本身慢、还会刷屏，拦下来让用户审批，用户能直接拒绝
DANGEROUS_PATTERNS = [
    r"(?<!git )(?<!npm )\brm\b",
    r"\bsudo\b",
    r"\bdd\b",
    r"\bmkfs\w*\b",
    r"\bfind\s+[~/]",
]
def run_command_self_check(args: dict):
    """
    run_command 的权限自检：扫一遍命令字符串，命中高危特征就要求审批。
    误伤的代价不过是多弹一次窗，绝不能把真正的高危命令漏过去。
    """
    command = args.get("command", "")
    if any(re.search(pattern, command) for pattern in DANGEROUS_PATTERNS):
        return "ask"
    # 没命中高危特征，交给通用规则决定
    return None


permissions.register_self_check("run_command", run_command_self_check)