"""
Coding Agent 用到的三个工具：读文件、写文件、跑 shell 命令。
"""
import subprocess
import re
import os
import permissions
from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ModelRetry
from .file_state import ReadFileState
# 不指定 limit 时最多读多少行，超出的截断并提示模型用 offset 续读
DEFAULT_MAX_LINES = 2000

def _with_line_numbers(content: str, start_line: int = 1) -> str:
    """
    给每行加上行号前缀，格式类似 cat -n：行号右对齐 + 一个制表符 + 原行内容。
    模型按行号定位代码，给 edit_file 的 old_string 才能对得准。
    start_line 是首行的行号；分段读取时它等于 offset，行号才能和文件对得上。
    """
    lines = content.splitlines()
    if not lines:
        return "(空文件)"
    # 按本段最大行号算对齐宽度（分段读取时行号从 start_line 起，不一定从 1 开始）
    width = len(str(start_line + len(lines) - 1))
    return "\n".join(f"{i:>{width}}\t{line}" for i, line in enumerate(lines, start_line))


def read_file(ctx: RunContext[ReadFileState], path: str, offset: int = 1, limit: int | None = None) -> str:
    """读取文件内容，输出带行号。大文件请用 offset/limit 分段读取。"""
    # 去重：上次 read_file 读过同一段、文件也没变过，不重复往上下文里塞内容
    record = ctx.deps.get(path)
    if record is not None and record.get("offset") is not None:
        if record["offset"] == offset and record["limit"] == limit:
            if os.path.getmtime(path) <= record["timestamp"]:
                return "文件内容没有变化，请参考上次 read_file 的结果，不必重复读取"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        raise ModelRetry(f"文件 {path} 不存在，请确认路径或换一个文件")

        # 按 offset/limit 切片：不指定 limit 时最多取 DEFAULT_MAX_LINES 行
    all_lines = content.splitlines()
    total = len(all_lines)
    start = offset - 1
    end = start + limit if limit is not None else start + DEFAULT_MAX_LINES
    selected = all_lines[start:end]
    selected_content = "\n".join(selected)
    truncated = limit is None and end < total

    # 登记进会话的 readFileState：存磁盘上的完整内容（edit_file 的唯一性检查需要全量快照），
    # 同时记录本次读取的 offset/limit，用于上面那段去重逻辑
    ctx.deps.record(path, content, offset=offset, limit=limit)

    # 返回给模型的是带行号的版本；如果被截断，末尾附提示
    result = _with_line_numbers(selected_content, start_line=offset)
    if truncated:
        result += f"\n\n（文件共 {total} 行，还有 {total - end} 行未显示。用 offset={end + 1} 继续读取）"
    return result
#
def edit_file(ctx: RunContext[ReadFileState], path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """
    在文件里精确替换字符串，只传改动的片段，省 token。
    编辑前必须先用 read_file 读取文件，否则会报错。
    修改已有文件时优先用 edit_file 而不是 write_file。
    old_string 必须在文件里唯一匹配；多处匹配时补充上下文使其唯一，或传 replace_all=true 全部替换。
    old_string 和 new_string 不要包含 read_file 输出的行号前缀。

    Args:
        path: 要编辑的文件路径
        old_string: 要被替换掉的原文片段，必须和文件里的内容逐字符一致（不含行号前缀）
        new_string: 替换后的新内容
        replace_all: 是否替换所有匹配项，默认只替换唯一的一处
    """
    # 1. 空操作：换了个寂寞，直接打回
    if old_string == new_string:
        raise ModelRetry("old_string 和 new_string 完全相同，这次编辑没有任何改动")

    # 2. 先读后写：没在 readFileState 里登记过，说明还没读就想改，打回让它先读
    record = ctx.deps.get(path)
    if record is None:
        raise ModelRetry(f"还没读过 {path}，请先用 read_file 读取它，再基于真实内容编辑")

    # 3. mtime 防覆盖：读完之后文件又被改过，基于旧内容改会覆盖掉别人的改动，打回重读
    try:
        current_mtime = os.path.getmtime(path)
    except FileNotFoundError:
        raise ModelRetry(f"{path} 已不存在，无法编辑")
    if current_mtime > record["timestamp"]:
        raise ModelRetry(f"{path} 在你读取之后被改动过，请重新 read_file 拿到最新内容再编辑")

    # 4. 唯一性检查：在读取时登记的内容快照里数 old_string 出现几次。找不到没法改；多处又没开 replace_all 改哪处会有歧义
    snapshot = record["content"]
    count = snapshot.count(old_string)
    if count == 0:
        raise ModelRetry("old_string 在文件里找不到，请对照 read_file 的输出确认原文")
    if count > 1 and not replace_all:
        raise ModelRetry(
            f"old_string 在文件里出现了 {count} 次，请补充上下文让它唯一，或传 replace_all=true 全部替换"
        )

    # 5. 校验都过了，再从磁盘读取最新内容做替换：校验和落盘之间隔着权限审批，审批期间文件可能被改，落盘以磁盘最新内容为准
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 6. 朴素的字符串替换，终端里红绿 diff 只是展示，真正写盘的是替换后的完整内容
    if replace_all:
        updated = content.replace(old_string, new_string)
    else:
        updated = content.replace(old_string, new_string, 1)

    # 7. 整体写回磁盘，再用新内容和新 mtime 刷新登记
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    ctx.deps.record(path, updated)
    return f"已编辑 {path}（替换 {count if replace_all else 1} 处）"


def write_file(ctx: RunContext[ReadFileState], path: str, content: str) -> str:
    """
    把内容整体写入文件，已有文件会被覆盖。
    覆盖已有文件前必须先用 read_file 读取过，否则会报错。
    修改已有文件优先用 edit_file（只传改动片段，省 token），write_file 只用于新建文件或整体重写。
    """
    # 覆盖已有文件：沿用 edit_file 那套先读后写约束，防止整体覆盖掉没读过的内容
    if os.path.exists(path):
        record = ctx.deps.get(path)
        if record is None:
            raise ModelRetry(f"{path} 已存在，覆盖前请先用 read_file 读一遍，确认不会误删内容")
        if os.path.getmtime(path) > record["timestamp"]:
            raise ModelRetry(f"{path} 在你读取之后被改动过，请重新 read_file 再覆盖")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except FileNotFoundError:
        raise ModelRetry(f"目录不存在，无法写入 {path}，请换一个已存在的目录")
    except PermissionError:
        raise ModelRetry(f"没有权限写入 {path}，请换一个可写的路径")
    except OSError as e:
        return f"错误：写入 {path} 失败 ({e})"
    # 新写入的内容同样登记进 readFileState，后续要再改就不必重读
    ctx.deps.record(path, content)
    return f"已写入 {path}"

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
DANGEROUS_PATTERNS = [
    r"(?<!git )(?<!npm )\brm\b",
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
# edit_file 和 write_file 标记 sequential=True：同一轮里的多个改文件调用必须串行执行，
# 否则它们会基于同一份旧快照并发写盘、互相覆盖（这正是 readFileState + mtime 想防住的并发问题）
TOOLS = [
    read_file,
    Tool(edit_file, sequential=True),
    Tool(write_file, sequential=True),
    run_command,
]