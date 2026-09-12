import fcntl
import os
import platform
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.fake_openai_server import FakeOpenAIServer
from tests.image_helpers import make_png

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PtyProcess:
    def __init__(self, command, cwd, env):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self.output = b""

    def send(self, data):
        os.write(self.master, data)

    def read_until(self, expected, timeout=30, start=0):
        expected_bytes = expected.encode() if isinstance(expected, str) else expected
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expected_bytes in self.output[start:]:
                return
            readable, _, _ = select.select([self.master], [], [], 0.25)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk
        tail = self.output[-1200:].decode(errors="replace")
        raise AssertionError(f"等待 {expected!r} 超时，终端末尾输出：\n{tail}")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        os.close(self.master)

    def wait_exit(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            return_code = self.process.poll()
            if return_code is not None:
                return return_code
            readable, _, _ = select.select([self.master], [], [], 0.25)
            if readable:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                self.output += chunk
        tail = self.output[-1200:].decode(errors="replace")
        raise AssertionError(f"REPL 没有退出，终端末尾输出：\n{tail}")


def write_fake_clipboard_tool(bin_dir: Path, image_path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        executable = bin_dir / "osascript"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import os, re, shutil, sys\n"
            "arguments = '\\n'.join(sys.argv[1:])\n"
            "if 'clipboard info' in arguments:\n"
            "    sys.stdout.write('PNGf\\n')\n"
            "else:\n"
            "    match = re.search(r'POSIX file \\\"([^\\\"]+)\\\"', arguments)\n"
            "    if match is None:\n"
            "        raise SystemExit(2)\n"
            "    shutil.copyfile(os.environ['E2E_CLIPBOARD_IMAGE'], match.group(1))\n",
        )
    elif system == "Linux":
        executable = bin_dir / "xclip"
        executable.write_text("#!/bin/sh\ncat \"$E2E_CLIPBOARD_IMAGE\"\n")
    else:
        pytest.skip(f"PTY 图片测试暂不支持 {system}")
    executable.chmod(0o755)


def find_request(requests, prompt):
    for request in requests:
        messages = request["payload"].get("messages", [])
        if prompt in str(messages):
            return request["payload"]
    raise AssertionError(f"没有找到包含 {prompt!r} 的请求")


def test_cli_paste_submit_escape_and_new_are_hermetic(tmp_path):
    if not hasattr(os, "openpty"):
        pytest.skip("当前平台没有 PTY")

    home_dir = tmp_path / "home"
    work_dir = tmp_path / "work"
    bin_dir = tmp_path / "bin"
    for directory in (home_dir, work_dir, bin_dir):
        directory.mkdir()
    image_path = make_png(tmp_path / "clipboard.png")
    write_fake_clipboard_tool(bin_dir, image_path)

    with FakeOpenAIServer(reply="CLI_IMAGE_OK") as server:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home_dir),
                "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                "PYTHONPATH": str(REPOSITORY_ROOT),
                "PYTHONUNBUFFERED": "1",
                "TERM": "xterm-256color",
                "DEEPSEEK_API_KEY": "local-test-key",
                "DEEPSEEK_API_BASE": server.base_url,
                "DEEPSEEK_MODEL": "test-vision",
                "E2E_CLIPBOARD_IMAGE": str(image_path),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        repl = PtyProcess([sys.executable, str(REPOSITORY_ROOT / "main.py")], work_dir, env)
        try:
            repl.read_until("Shift+Tab", timeout=45)
            first_mark = len(repl.output)
            repl.send(b"\x16")
            repl.read_until("[Image #1]", start=first_mark)
            repl.read_until("1 张图片待发送", start=first_mark)

            repl.send(b"describe image\r")
            repl.read_until("CLI_IMAGE_OK", timeout=45, start=first_mark)
            first_request = find_request(server.requests, "describe image")
            assert "image_url" in str(first_request["messages"])

            second_mark = len(repl.output)
            repl.read_until("Shift+Tab", timeout=30, start=second_mark)
            repl.send(b"\x16")
            repl.read_until("[Image #1]", start=second_mark)
            repl.send(b"\x1b")
            repl.send(b"plain second\r")
            repl.read_until("CLI_IMAGE_OK", timeout=45, start=second_mark)
            second_request = find_request(server.requests, "plain second")
            first_image_count = str(first_request["messages"]).count("image_url")
            second_image_count = str(second_request["messages"]).count("image_url")
            latest_user = [message for message in second_request["messages"] if message["role"] == "user"][-1]
            assert latest_user["content"] == "plain second"
            assert second_image_count == first_image_count

            final_mark = len(repl.output)
            repl.read_until("Shift+Tab", timeout=30, start=final_mark)
            repl.send(b"/new\r")
            repl.read_until("已开启新会话", timeout=30, start=final_mark)
            exit_mark = len(repl.output)
            repl.read_until("Shift+Tab", timeout=30, start=exit_mark)
            repl.send(b"/exit\r")
            assert repl.wait_exit(timeout=30) == 0
        finally:
            repl.close()

    assert not list(home_dir.rglob("clipboard-*.png"))
