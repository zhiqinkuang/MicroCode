import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import BinaryContent

import images
from tests.image_helpers import sample_bytes


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.png", True),
        ("b.JPG", True),
        ("c.jpeg", True),
        ("d.GIF", True),
        ("e.webp", True),
        ("noext", False),
        ("movie.mp4", False),
    ],
)
def test_is_image_uses_supported_extensions(filename, expected):
    assert images.is_image(filename) is expected


@pytest.mark.parametrize(
    ("suffix", "media_type"),
    [(".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".gif", "image/gif"), (".webp", "image/webp")],
)
def test_load_image_validates_and_returns_binary_content(tmp_path, suffix, media_type):
    path = tmp_path / f"sample{suffix}"
    path.write_bytes(sample_bytes(media_type))

    result = images.load_image(path)

    assert isinstance(result, BinaryContent)
    assert result.data == sample_bytes(media_type)
    assert result.media_type == media_type


def test_load_image_rejects_unsupported_extension(tmp_path):
    path = tmp_path / "sample.bmp"
    path.write_bytes(b"BMpayload")

    with pytest.raises(images.ImageInputError, match="不支持"):
        images.load_image(path)


def test_load_image_reports_missing_file(tmp_path):
    with pytest.raises(images.ImageInputError, match="不存在"):
        images.load_image(tmp_path / "missing.png")


def test_load_image_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")

    with pytest.raises(images.ImageInputError, match="空"):
        images.load_image(path)


def test_load_image_rejects_oversized_file(tmp_path, monkeypatch):
    path = tmp_path / "large.png"
    path.write_bytes(sample_bytes("image/png"))
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 4)

    with pytest.raises(images.ImageInputError, match="超过"):
        images.load_image(path)


def test_load_image_rejects_corrupt_data(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not-an-image")

    with pytest.raises(images.ImageInputError, match="无法识别"):
        images.load_image(path)


def test_load_image_rejects_extension_signature_mismatch(tmp_path):
    path = tmp_path / "wrong.png"
    path.write_bytes(sample_bytes("image/jpeg"))

    with pytest.raises(images.ImageInputError, match="扩展名"):
        images.load_image(path)


def test_build_user_content_keeps_text_image_order(tmp_path):
    first = BinaryContent(data=b"first", media_type="image/png")
    second = BinaryContent(data=b"second", media_type="image/png")

    result = images.build_user_content("看[Image #2]再看[Image #1]。", [first, second])

    assert result == ["看", second, "再看", first, "。"]


def test_build_user_content_keeps_invalid_placeholder_and_appends_unused_images():
    attachment = BinaryContent(data=b"image", media_type="image/png")

    result = images.build_user_content("[Image #9] 保留", [attachment])

    assert result == ["[Image #9] 保留", attachment]


def test_build_user_content_allows_intentional_repeated_reference():
    attachment = BinaryContent(data=b"image", media_type="image/png")

    result = images.build_user_content("[Image #1]和[Image #1]", [attachment])

    assert result == [attachment, "和", attachment]


def test_append_attachment_returns_number_and_enforces_limit(monkeypatch):
    monkeypatch.setattr(images, "MAX_ATTACHMENTS", 1)
    attachments = []
    content = BinaryContent(data=b"image", media_type="image/png")

    assert images.append_attachment(attachments, content) == 1
    with pytest.raises(images.ImageInputError, match="最多"):
        images.append_attachment(attachments, content)
    assert attachments == [content]


@pytest.mark.parametrize(
    ("system", "stdout"),
    [("Darwin", b""), ("Linux", b"0\n"), ("Windows", b"False\r\n")],
)
def test_clipboard_no_image_returns_none(tmp_path, system, stdout):
    def run_command(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    result = images.read_clipboard_image(system=system, run_command=run_command, clipboard_dir=tmp_path)

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_clipboard_rejects_unsupported_system(tmp_path):
    with pytest.raises(images.ImageInputError, match="不支持"):
        images.read_clipboard_image(system="Plan9", clipboard_dir=tmp_path)


@pytest.mark.parametrize(
    "failure",
    [
        SimpleNamespace(returncode=2, stdout=b"", stderr=b"failed"),
        OSError("missing command"),
        subprocess.TimeoutExpired(["clipboard"], 5),
    ],
)
def test_clipboard_probe_failure_is_actionable(tmp_path, failure):
    def run_command(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    with pytest.raises(images.ImageInputError, match="剪贴板"):
        images.read_clipboard_image(system="Darwin", run_command=run_command, clipboard_dir=tmp_path)


def test_clipboard_save_failure_is_actionable_and_cleans_temp_file(tmp_path):
    calls = 0

    def run_command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout=b"PNGf", stderr=b"")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"save failed")

    with pytest.raises(images.ImageInputError, match="保存"):
        images.read_clipboard_image(system="Darwin", run_command=run_command, clipboard_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_clipboard_success_loads_image_and_cleans_temp_file(tmp_path):
    calls = 0

    def run_command(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout=b"PNGf", stderr=b"")
        path = _extract_output_path(command, "Darwin")
        path.write_bytes(sample_bytes("image/png"))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    result = images.read_clipboard_image(system="Darwin", run_command=run_command, clipboard_dir=tmp_path)

    assert result.media_type == "image/png"
    assert result.data == sample_bytes("image/png")
    assert list(tmp_path.iterdir()) == []


def test_clipboard_invalid_saved_image_is_reported_and_cleaned(tmp_path):
    calls = 0

    def run_command(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout=b"PNGf", stderr=b"")
        path = _extract_output_path(command, "Darwin")
        path.write_bytes(b"broken")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with pytest.raises(images.ImageInputError, match="无法识别"):
        images.read_clipboard_image(system="Darwin", run_command=run_command, clipboard_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_contains_image_only_matches_image_blocks():
    image = BinaryContent(data=sample_bytes("image/png"), media_type="image/png")
    assert images.contains_image(image) is True
    assert images.contains_image(["看这张图", image, "补充说明"]) is True
    assert images.contains_image("纯文本提问") is False
    assert images.contains_image(["只有文字", "还是文字"]) is False
    assert images.contains_image([]) is False


def test_summarize_content_handles_runtime_and_serialized_images():
    runtime_image = BinaryContent(data=b"x" * 2048, media_type="image/png")
    assert images.summarize_content("纯文本") == "纯文本"
    assert images.summarize_content(["看图", runtime_image]) == "看图 [图片 image/png，2 KB]"

    serialized = {"kind": "binary", "data": "eA==" * 1024, "media_type": "image/jpeg"}
    summary = images.summarize_content(["读过了", serialized])
    assert summary.startswith("读过了 [图片 image/jpeg，")
    assert "eA==" not in summary


def test_summarize_content_never_exposes_unknown_binary_fields():
    unknown = {"kind": "binary", "data": "c2VjcmV0LXBheWxvYWQ=", "media_type": "application/pdf"}
    assert "c2VjcmV0LXBheWxvYWQ=" not in images.summarize_content([unknown])
    assert images.summarize_content([{"kind": "other", "data": "c2VjcmV0"}]) == "[未知内容块]"
    assert images.summarize_content([{"text": "保留文本"}]) == "保留文本"


def _extract_output_path(command: list[str], system: str) -> Path:
    if system == "Darwin":
        marker = 'POSIX file "'
        script = next(part for part in command if marker in part)
        return Path(script.split(marker, 1)[1].split('"', 1)[0])
    if system == "Linux":
        script = command[-1]
        return Path(script.rsplit('"', 2)[1])
    script = command[-1]
    return Path(script.split(".Save('", 1)[1].split("'", 1)[0])
