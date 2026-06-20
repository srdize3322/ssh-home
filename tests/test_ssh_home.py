from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "ssh-home.py"
SPEC = importlib.util.spec_from_file_location("ssh_home", MODULE_PATH)
ssh_home = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = ssh_home
SPEC.loader.exec_module(ssh_home)


class PromptTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class PromptPipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class FakeTTY:
    def __init__(self, scripted_input: str) -> None:
        self._lines = scripted_input.splitlines(keepends=True)
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.pop(0)


class ParseHostsTests(unittest.TestCase):
    def test_parse_hosts_handles_include_and_filters_wildcards(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        hosts = ssh_home.parse_hosts_from_configs(config_path)
        self.assertEqual(
            hosts,
            ["app-prod", "app-stage", "proxied", "gateway", "nested-a", "nested-b"],
        )

    def test_iter_ssh_config_files_follows_include_relative_to_parent(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        files = ssh_home.iter_ssh_config_files(config_path)
        self.assertEqual(
            [path.name for path in files],
            ["ssh_config", "extra.conf"],
        )

    def test_filter_candidates_is_case_insensitive(self) -> None:
        self.assertEqual(
            ssh_home.filter_candidates(["Proxmox", "workspace", "Jelly"], "ox"),
            ["Proxmox"],
        )

    def test_parse_config_line_respects_quotes_and_comments(self) -> None:
        self.assertEqual(
            ssh_home.parse_config_line('Host "app prod" app-stage # comment'),
            ["Host", "app prod", "app-stage"],
        )

    def test_effective_config_flattens_includes_without_re_emitting_include(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        with ssh_home.EffectiveSSHConfig(config_path) as effective_config:
            text = effective_config.read_text(encoding="utf-8")
        self.assertNotIn("Include ssh_config.d/*.conf", text)
        self.assertIn("Host gateway", text)


class ResolutionTests(unittest.TestCase):
    def test_resolve_host_uses_ssh_dash_g(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        resolved = ssh_home.resolve_host("proxied", config_path)
        self.assertEqual(resolved.hostname, "198.51.100.7")
        self.assertEqual(resolved.user, "root")
        self.assertEqual(resolved.port, "22")
        self.assertEqual(resolved.proxyjump, "gateway")


class PromptIOTests(unittest.TestCase):
    def test_prompt_io_uses_stdout_for_writes_when_both_streams_are_tty(self) -> None:
        stdin_tty = PromptTTY("1\n")
        stdout_tty = PromptTTY()
        io_helper = ssh_home.PromptIO(stdin=stdin_tty, stdout=stdout_tty)
        io_helper.println("Hosts SSH detectados:")
        answer = io_helper.prompt("elige: ")
        self.assertEqual(answer, "1")
        self.assertEqual(stdin_tty.getvalue(), "1\n")
        self.assertEqual(stdout_tty.getvalue(), "Hosts SSH detectados:\nelige: ")

    def test_prompt_io_reopens_tty_when_stdin_is_not_interactive(self) -> None:
        tty = FakeTTY("2\n")
        io_helper = ssh_home.PromptIO(
            stdin=PromptPipe(""),
            stdout=PromptPipe(""),
            tty_factory=lambda: tty,
        )
        answer = io_helper.prompt("elige: ")
        self.assertEqual(answer, "2")
        self.assertEqual("".join(tty.writes), "elige: ")


class RemoteBrowsingTests(unittest.TestCase):
    def test_list_remote_directories_parses_expected_format(self) -> None:
        session = mock.Mock()
        session.run_capture.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/srv/app\n__SSH_HOME_DIRS__\nconfig\nreleases\n",
            stderr="",
        )
        current, directories = ssh_home.list_remote_directories(session, "/srv/app")
        self.assertEqual(current, "/srv/app")
        self.assertEqual(directories, ["config", "releases"])

    def test_browse_remote_path_allows_entering_directories_and_confirming(self) -> None:
        session = mock.Mock()
        session.alias = "app-prod"
        session.run_capture.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="/srv\n__SSH_HOME_DIRS__\napp\nlogs\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="/srv/app\n__SSH_HOME_DIRS__\nconfig\n",
                stderr="",
            ),
        ]

        tty = FakeTTY("1\nc\n")
        io_helper = ssh_home.PromptIO(
            stdin=tty,
            stdout=tty,
            tty_factory=lambda: tty,
        )
        selected = ssh_home.browse_remote_path(session, io_helper)
        self.assertEqual(selected, "/srv/app")

    def test_browse_remote_path_manual_relative_path(self) -> None:
        session = mock.Mock()
        session.alias = "app-prod"
        session.run_capture.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="/srv\n__SSH_HOME_DIRS__\napp\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="/srv/shared\n__SSH_HOME_DIRS__\ncurrent\n",
                stderr="",
            ),
        ]

        tty = FakeTTY("m\nshared\nc\n")
        io_helper = ssh_home.PromptIO(
            stdin=tty,
            stdout=tty,
            tty_factory=lambda: tty,
        )
        selected = ssh_home.browse_remote_path(session, io_helper)
        self.assertEqual(selected, "/srv/shared")

    def test_browse_remote_path_raises_when_remote_listing_fails(self) -> None:
        session = mock.Mock()
        session.alias = "app-prod"
        session.run_capture.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="permission denied",
        )

        tty = PromptTTY("")
        io_helper = ssh_home.PromptIO(
            stdin=tty,
            stdout=tty,
            tty_factory=lambda: tty,
        )
        with self.assertRaises(ssh_home.SSHHomeError):
            ssh_home.browse_remote_path(session, io_helper)


class CLIListingTests(unittest.TestCase):
    def test_run_list_outputs_hosts(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        with mock.patch("sys.stdout", new=io.StringIO()) as buffer:
            exit_code = ssh_home.run(["--list", "--config", str(config_path)])
        self.assertEqual(exit_code, 0)
        self.assertIn("app-prod", buffer.getvalue())

    def test_choose_host_interactively_accepts_q_to_cancel(self) -> None:
        tty = FakeTTY("q\n")
        io_helper = ssh_home.PromptIO(stdin=tty, stdout=tty, tty_factory=lambda: tty)
        with self.assertRaises(ssh_home.SSHHomeError):
            ssh_home.choose_host_interactively(["app-prod"], io_helper)

    def test_run_with_host_and_path_does_not_require_prompt_io(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        instances = []

        class FakeSession:
            def __init__(self, alias, config_path):
                self.alias = alias
                self.config_path = config_path
                self.remote_script = ""
                self.closed = False
                instances.append(self)

            def run_interactive(self, remote_script):
                self.remote_script = remote_script
                return 7

            def close(self):
                self.closed = True

        with mock.patch.object(ssh_home, "PromptIO", side_effect=AssertionError):
            with mock.patch.object(ssh_home, "SSHMasterSession", FakeSession):
                exit_code = ssh_home.run(
                    [
                        "--host",
                        "app-prod",
                        "--path",
                        "/srv/app",
                        "--config",
                        str(config_path),
                    ]
                )

        self.assertEqual(exit_code, 7)
        self.assertEqual(len(instances), 1)
        self.assertIn("/srv/app", instances[0].remote_script)
        self.assertTrue(instances[0].closed)


class TUIHelperTests(unittest.TestCase):
    def test_is_enter_accepts_newline_string(self) -> None:
        ui = object.__new__(ssh_home.SSHHomeTUI)
        self.assertTrue(ui._is_enter("\n"))
        self.assertTrue(ui._is_enter("\r"))


if __name__ == "__main__":
    unittest.main()
