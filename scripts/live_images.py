"""Opt-in release smoke test for image input using the real configured models.

Run: PYTHONPATH=. no_proxy=api.deepseek.com uv run python scripts/live_images.py --repeat 3

这是发布前的真实模型检查，会消耗 API 用量；pytest / CI 永不运行它。
每个 --repeat 编号都是一次独立记录：不做隐式重试，任一场景失败即以非零码退出并点名失败的重复编号。
"""
import argparse
import asyncio
import logging
import os
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

logger = logging.getLogger("live_images")

SCENARIOS = ("direct", "read_file", "mention")
EXPECTED_COLORS = ("红", "蓝")
EXCERPT_LIMIT = 300
ATTEMPT_TIMEOUT_SECONDS = 180


@dataclass
class Attempt:
    """一次场景执行的记录；失败只记录原因，不在内部重试。"""

    repeat_index: int
    scenario: str
    model_name: str
    elapsed_seconds: float
    image_block: bool
    excerpt: str = ""
    error: str = ""

    @property
    def label(self) -> str:
        return f"repeat {self.repeat_index} / {self.scenario}"

    def describe(self) -> str:
        status = "PASS" if not self.error else "FAIL"
        return (
            f"{status} {self.label}：model={self.model_name or '未知'}"
            f"，image_block={self.image_block}"
            f"，elapsed={self.elapsed_seconds:.2f}s"
            + (f"，error={self.error}" if self.error else "")
            + (f"，excerpt={self.excerpt!r}" if self.excerpt else "")
        )


def make_half_png(path: Path, left_rgb=(255, 0, 0), right_rgb=(0, 0, 255), size: int = 64) -> Path:
    """手写一张左半 left_rgb、右半 right_rgb 的 PNG，不依赖第三方图像库。"""
    half = size // 2
    row = bytes(left_rgb) * half + bytes(right_rgb) * (size - half)
    raw = b"".join(b"\x00" + row for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    return path


def history_text(state) -> str:
    """只取历史里的文本 part：图片字节永远不进日志。"""
    chunks = []
    for message in state.history:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks)


def history_has_tool_image(state) -> bool:
    from pydantic_ai.messages import BinaryContent, ToolReturnPart

    return any(
        isinstance(part, ToolReturnPart) and isinstance(part.content, BinaryContent)
        for message in state.history
        for part in message.parts
    )


def model_label(model) -> str:
    return str(getattr(model, "model_name", None) or type(model).__name__)


async def run_scenario(scenario: str, image_path: Path, work_dir: Path):
    """执行单个场景，返回 (模型名, 是否组装出图片块, 答案文本)。"""
    import images
    import main
    import permissions
    import session
    from agent import MODEL_NAME, VISION_MODEL_NAME, select_turn_model
    from memory import background as memory_background
    from UI.commands import SessionState

    state = SessionState(
        model_name=MODEL_NAME,
        vision_model_name=VISION_MODEL_NAME,
        session_id=session.new_session_id(),
    )
    configured_model = None
    image_block = False

    with (
        patch.object(permissions, "state", permissions.PermissionState(mode=permissions.BYPASS)),
        patch.object(memory_background, "schedule"),
        (work_dir / f"console-{scenario}.log").open("w", encoding="utf-8") as console_file,
        patch.object(main.console, "_file", console_file),
    ):
        try:
            if scenario == "direct":
                content = images.build_user_content(
                    "这张图片左半边和右半边各是什么颜色？请只回答颜色。",
                    [images.load_image(image_path)],
                )
            elif scenario == "read_file":
                content = (
                    f"用 read_file 读取 {image_path}，然后告诉我图片左半边和右半边各是什么颜色。"
                )
            elif scenario == "mention":
                previous_dir = Path.cwd()
                os.chdir(work_dir)
                try:
                    attachments = []
                    replaced = main.inject_at_mentions(
                        f"@{image_path.name} 这张图左右各是什么颜色？", state, attachments
                    )
                    content = images.build_user_content(replaced, attachments)
                finally:
                    os.chdir(previous_dir)
            else:  # pragma: no cover - argparse 只会给出 SCENARIOS 里的值
                raise ValueError(f"未知场景：{scenario}")

            image_block = images.contains_image(content) or bool(getattr(state, "attachments", []))
            # 三个场景都走产品路由：含图片块的一轮用视觉模型；read_file 场景以纯文本开场、
            # 图片由工具返回，考验的正是所配置 DEEPSEEK_MODEL 的视觉能力（2026-09-12 实测
            # deepseek-v4-flash 能正确读图，见 README「模型路由」）。
            selected = select_turn_model(content)
            configured_model = model_label(selected)

            await main.run_agent_loop(content, state, model=selected)
            image_block = image_block or history_has_tool_image(state)
            return configured_model, image_block, history_text(state)
        finally:
            await state.job_registry.aclose()


async def run_attempt(repeat_index: int, scenario: str, image_path: Path, work_dir: Path) -> Attempt:
    started = time.monotonic()
    attempt = Attempt(
        repeat_index=repeat_index,
        scenario=scenario,
        model_name="",
        elapsed_seconds=0.0,
        image_block=False,
    )
    try:
        async with asyncio.timeout(ATTEMPT_TIMEOUT_SECONDS):
            model_name, image_block, answer = await run_scenario(scenario, image_path, work_dir)
        attempt.model_name = model_name
        attempt.image_block = image_block
        attempt.excerpt = answer[-EXCERPT_LIMIT:]
        if not image_block:
            attempt.error = "本轮没有组装出图片块，图片没有真正进入请求"
        elif not all(color in answer for color in EXPECTED_COLORS):
            attempt.error = f"模型没有认出左红右蓝，期望同时出现 {EXPECTED_COLORS}"
    except Exception as exc:  # noqa: BLE001 - 真实发布检查要把任何异常都记录成一次失败
        attempt.error = f"{type(exc).__name__}: {exc}"
    finally:
        attempt.elapsed_seconds = time.monotonic() - started
    return attempt


async def run_all(project_dir: Path, repeat: int) -> list[Attempt]:
    sys.path.insert(0, str(project_dir))
    import session
    from agent import MODEL_NAME, VISION_MODEL_NAME

    logger.info(
        "真实图片冒烟：%s 次 × %s 个场景；文本模型=%s，视觉模型=%s",
        repeat,
        len(SCENARIOS),
        MODEL_NAME,
        VISION_MODEL_NAME or "未配置",
    )
    attempts: list[Attempt] = []
    with tempfile.TemporaryDirectory(prefix="live-images-") as directory:
        root_dir = Path(directory).resolve()
        image_path = make_half_png(root_dir / "half.png")
        with patch.object(session, "STORAGE_ROOT", root_dir / "projects"):
            for repeat_index in range(1, repeat + 1):
                for scenario in SCENARIOS:
                    attempt = await run_attempt(repeat_index, scenario, image_path, root_dir)
                    attempts.append(attempt)
                    logger.info(attempt.describe())
    return attempts


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=1, help="每个场景重复次数，编号从 1 开始")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logger.setLevel(logging.INFO)

    attempts = asyncio.run(run_all(args.project.resolve(), max(args.repeat, 1)))
    failures = [attempt for attempt in attempts if attempt.error]
    if failures:
        logger.error(
            "图片冒烟失败 %s/%s 次，失败编号：%s",
            len(failures),
            len(attempts),
            ", ".join(attempt.label for attempt in failures),
        )
        raise SystemExit(1)
    logger.info("图片冒烟全部通过：%s 次记录，%s 个场景", len(attempts), len(SCENARIOS))


if __name__ == "__main__":
    cli()
