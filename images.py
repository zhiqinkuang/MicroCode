"""图片输入的验证、组装和系统剪贴板读取。"""

import logging
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from pydantic_ai.messages import BinaryContent

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 8
CLIPBOARD_DIR = Path.home() / ".my-claude-code" / "clipboard"
IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image #(\d+)\]")


class ImageInputError(ValueError):
    """图片输入无法安全读取时返回给用户的错误。"""


def is_image(path: str | os.PathLike[str]) -> bool:
    """按扩展名判断路径是否属于支持的图片格式。"""
    return os.path.splitext(os.fspath(path))[1].lower() in IMAGE_EXTENSIONS


def detect_media_type(data: bytes) -> str | None:
    """根据文件签名识别支持的图片媒体类型。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_image(path: str | os.PathLike[str]) -> BinaryContent:
    """校验并读取本地图片，返回可交给 pydantic-ai 的内容块。"""
    image_path = Path(path)
    expected_media_type = IMAGE_EXTENSIONS.get(image_path.suffix.lower())
    if expected_media_type is None:
        supported = ", ".join(sorted(IMAGE_EXTENSIONS))
        raise ImageInputError(f"不支持的图片格式：{image_path}；支持 {supported}")
    try:
        with image_path.open("rb") as image_file:
            data = image_file.read(MAX_IMAGE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ImageInputError(f"图片不存在：{image_path}；请确认路径") from exc
    except OSError as exc:
        raise ImageInputError(f"无法读取图片 {image_path}：{exc}") from exc

    if not data:
        raise ImageInputError(f"图片是空文件：{image_path}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB 限制：{image_path}")

    actual_media_type = detect_media_type(data)
    if actual_media_type is None:
        raise ImageInputError(f"无法识别图片内容：{image_path}；文件可能已损坏")
    if actual_media_type != expected_media_type:
        raise ImageInputError(
            f"图片扩展名与内容不一致：{image_path}（扩展名表示 {expected_media_type}，实际为 {actual_media_type}）"
        )
    return BinaryContent(data=data, media_type=actual_media_type)


def append_attachment(attachments: list[BinaryContent], content: BinaryContent) -> int:
    """在容量允许时追加附件并返回一位起始编号。"""
    if len(attachments) >= MAX_ATTACHMENTS:
        raise ImageInputError(f"每轮最多发送 {MAX_ATTACHMENTS} 张图片")
    attachments.append(content)
    return len(attachments)


def build_user_content(text: str, attachments: list[BinaryContent]) -> list[str | BinaryContent]:
    """按占位符位置组装图文内容，未引用附件追加到末尾。"""
    parts: list[str | BinaryContent] = []
    referenced: set[int] = set()
    position = 0
    for match in IMAGE_PLACEHOLDER_RE.finditer(text):
        number = int(match.group(1))
        if not 1 <= number <= len(attachments):
            continue
        if match.start() > position:
            parts.append(text[position:match.start()])
        parts.append(attachments[number - 1])
        referenced.add(number)
        position = match.end()
    if position < len(text):
        parts.append(text[position:])
    for number, attachment in enumerate(attachments, 1):
        if number not in referenced:
            parts.append(attachment)
    return [part for part in parts if part != ""]


def contains_image(content) -> bool:
    """判断一轮用户输入是否夹带图片块；纯文本输入（str）直接返回 False。"""
    if isinstance(content, (list, tuple)):
        return any(isinstance(part, BinaryContent) for part in content)
    return isinstance(content, BinaryContent)


def summarize_content(content) -> str:
    """
    把一轮用户输入压成可读摘要：文本原样保留，图片块只留媒体类型与大小。

    content 可能是 str、运行时的 BinaryContent 列表，或从会话文件读回的序列化图片字典
    （{"kind": "binary", "data": <base64>, "media_type": ...}）；无论哪种形态都绝不把
    base64 或原始字节带进终端与会话列表。终端回放和会话列表摘要共用这一份规则。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return str(content)
    return " ".join(_summarize_item(item) for item in content)


def _summarize_item(item) -> str:
    if _is_image_block(item):
        media_type = item.media_type if isinstance(item, BinaryContent) else item.get("media_type", "")
        size_kb = _image_size_kb(item)
        suffix = f"，{size_kb:.0f} KB" if size_kb is not None else ""
        return f"[图片 {media_type}{suffix}]"
    if isinstance(item, dict):
        # 未知字典按文本处理，但绝不回显 data 字段：它可能是另一种二进制载荷
        for key in ("text", "content", "value"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return "[未知内容块]"
    return str(item)


def _is_image_block(item) -> bool:
    if isinstance(item, BinaryContent):
        return True
    return isinstance(item, dict) and (
        item.get("kind") == "binary" or str(item.get("media_type", "")).startswith("image/")
    )


def _image_size_kb(item) -> float | None:
    if isinstance(item, BinaryContent):
        return len(item.data) / 1024
    data = item.get("data")
    if isinstance(data, str):
        # base64 每 4 个字符还原 3 字节，等号是填充
        return (len(data) - data.count("=")) * 3 / 4 / 1024
    return None


_CHECK_COMMANDS = {
    "Darwin": ["osascript", "-e", "clipboard info for «class PNGf»"],
    "Linux": ["sh", "-c", "xclip -selection clipboard -t image/png -o | wc -c"],
    "Windows": ["powershell", "-NoProfile", "-Command", "(Get-Clipboard -Format Image) -ne $null"],
}


def _save_command(path: str, system: str | None = None) -> list[str]:
    """构造当前平台将剪贴板 PNG 写入临时文件的命令。"""
    selected_system = system or platform.system()
    commands = {
        "Darwin": [
            "osascript",
            "-e",
            "set png_data to (the clipboard as «class PNGf»)",
            "-e",
            f'set fp to open for access POSIX file "{path}" with write permission',
            "-e",
            "set eof fp to 0",
            "-e",
            "write png_data to fp",
            "-e",
            "close access fp",
        ],
        "Linux": ["sh", "-c", f'xclip -selection clipboard -t image/png -o > "{path}"'],
        "Windows": [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$img = Get-Clipboard -Format Image; if ($img) {{ $img.Save('{path}') }}",
        ],
    }
    return commands[selected_system]


def _clipboard_has_image(system: str, stdout: bytes) -> bool:
    output = stdout.strip()
    if system == "Windows":
        return output.lower() == b"true"
    if system == "Linux":
        try:
            return int(output or b"0") > 0
        except ValueError:
            return False
    return bool(output) and output.lower() != b"false"


def _command_error(prefix: str, result) -> ImageInputError:
    detail = getattr(result, "stderr", b"")
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    detail = str(detail).strip()[:200]
    return ImageInputError(f"{prefix}{f'：{detail}' if detail else ''}")


def read_clipboard_image(
    *,
    system: str | None = None,
    run_command: Callable = subprocess.run,
    clipboard_dir: Path | None = None,
) -> BinaryContent | None:
    """从系统剪贴板读取图片；没有图片返回 ``None``，操作失败抛出错误。"""
    selected_system = system or platform.system()
    check_command = _CHECK_COMMANDS.get(selected_system)
    if check_command is None:
        raise ImageInputError(f"当前系统不支持剪贴板图片：{selected_system}")

    try:
        probe = run_command(check_command, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageInputError(f"无法读取系统剪贴板：{exc}") from exc
    if probe.returncode != 0:
        raise _command_error("无法读取系统剪贴板", probe)
    if not _clipboard_has_image(selected_system, probe.stdout):
        return None

    target_dir = clipboard_dir or CLIPBOARD_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageInputError(f"无法创建剪贴板临时目录：{exc}") from exc

    descriptor, raw_path = tempfile.mkstemp(prefix="clipboard-", suffix=".png", dir=target_dir)
    os.close(descriptor)
    temporary_path = Path(raw_path)
    try:
        try:
            saved = run_command(_save_command(raw_path, selected_system), capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageInputError(f"无法保存剪贴板图片：{exc}") from exc
        if saved.returncode != 0:
            raise _command_error("无法保存剪贴板图片", saved)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise ImageInputError("无法保存剪贴板图片：没有生成有效文件")
        return load_image(temporary_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("无法清理剪贴板临时文件：%s", temporary_path)
