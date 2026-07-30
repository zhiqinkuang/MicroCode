"""
Coding Agent 用到的三个工具：读文件、写文件、跑 shell 命令。
"""
import subprocess
import re
import permissions

def  read_file(path:str) -> str:
    """
    读取指定文件的内容。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"错误：文件 {path} 不存在"

def write_file(path:str,content:str):
    """
    将内容写入指定文件。
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}"

def run_command(command: str) -> str:
    """
    执行一条 shell 命令并返回输出。
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, errors="replace", timeout=10
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\n[错误] {result.stderr}"
        return output or "(无输出)"
    except subprocess.TimeoutExpired:
        return "[错误] 命令执行超时（10秒）"




# 高危命令的特征：删除文件、提权、直写磁盘
DANGEROUS_PATTERNS = [
    r"\brm\b",
    r"\bsudo\b",
    r"\bdd\b",
    r"\bmkfs\w*\b",
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


# Pydantic AI 支持 tools=[plain_function]，从函数签名 + docstring 自动生成 JSON Schema
TOOLS = [read_file, write_file, run_command]