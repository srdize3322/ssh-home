from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
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
            ssh_home.filter_candidates(["Gateway", "workspace", "Jelly"], "way"),
            ["Gateway"],
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
        self.assertIn("id_test", resolved.identityfile)


class AddEndpointTests(unittest.TestCase):
    def test_format_ssh_host_entry_writes_only_present_fields(self) -> None:
        entry = ssh_home.SSHHostEntry(
            alias="media-box",
            hostname="203.0.113.20",
            user="admin",
            port="2222",
            identity_file="~/.ssh/id_media",
        )

        self.assertEqual(
            ssh_home.format_ssh_host_entry(entry),
            "\n".join(
                [
                    "Host media-box",
                    "    HostName 203.0.113.20",
                    "    User admin",
                    "    Port 2222",
                    "    IdentityFile ~/.ssh/id_media",
                    "",
                ]
            ),
        )

    def test_append_ssh_host_entry_creates_config_and_parses_new_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "ssh" / "config"
            entry = ssh_home.SSHHostEntry(
                alias="lab-box",
                hostname="203.0.113.30",
                user="ubuntu",
            )

            ssh_home.append_ssh_host_entry(config_path, entry)

            text = config_path.read_text(encoding="utf-8")
            self.assertIn("# Added by ssh-home", text)
            self.assertIn("Host lab-box", text)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(ssh_home.parse_hosts_from_configs(config_path), ["lab-box"])

    def test_append_ssh_host_entry_rejects_duplicate_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config"
            config_path.write_text(
                "Host app-prod\n    HostName 203.0.113.10\n",
                encoding="utf-8",
            )

            with self.assertRaises(ssh_home.SSHHomeError):
                ssh_home.append_ssh_host_entry(
                    config_path,
                    ssh_home.SSHHostEntry(alias="app-prod", hostname="203.0.113.11"),
                )

    def test_run_add_endpoint_accepts_noninteractive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config"
            with mock.patch("sys.stdout", new=io.StringIO()) as buffer:
                exit_code = ssh_home.run(
                    [
                        "--add",
                        "--config",
                        str(config_path),
                        "--add-alias",
                        "edge-box",
                        "--hostname",
                        "203.0.113.40",
                        "--ssh-user",
                        "deploy",
                        "--port",
                        "2200",
                        "--no-state",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("edge-box", buffer.getvalue())
            self.assertIn("Host edge-box", config_path.read_text(encoding="utf-8"))


class StateTests(unittest.TestCase):
    def test_state_load_creates_default_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state = ssh_home.SSHHomeState.load(state_path)
            self.assertTrue(state_path.exists())
            self.assertEqual(state.favorites, [])
            self.assertEqual(state.preference("view", "bad"), ssh_home.HOST_VIEW_ALL)

    def test_state_load_recovers_from_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text("{not-json", encoding="utf-8")
            state = ssh_home.SSHHomeState.load(state_path)
            self.assertEqual(state.favorites, [])
            self.assertEqual(state.recents, {})

    def test_disabled_state_never_writes(self) -> None:
        state = ssh_home.SSHHomeState.disabled()
        state.toggle_favorite("app-prod")
        state.record_connection("app-prod", "/srv/app")
        state.save()
        self.assertFalse(state.is_favorite("app-prod"))
        self.assertIsNone(state.last_path("app-prod"))

    def test_state_tracks_favorites_recents_and_history(self) -> None:
        state = ssh_home.SSHHomeState(path=None, enabled=True)
        self.assertTrue(state.toggle_favorite("app-prod"))
        state.data["recents"] = {"app-stage": 10.0, "gateway": 20.0}
        state.data["last_paths"] = {"app-stage": "/srv/stage", "gateway": "/srv/gateway"}
        ordered = ssh_home.ordered_hosts(
            ["gateway", "app-stage", "app-prod", "nested-a"],
            state,
            ssh_home.HOST_VIEW_ALL,
        )
        self.assertEqual(ordered[:3], ["app-prod", "gateway", "app-stage"])
        self.assertEqual(state.last_path("app-stage"), "/srv/stage")

    def test_clear_history_keeps_favorites(self) -> None:
        state = ssh_home.SSHHomeState(path=None, enabled=True)
        state.toggle_favorite("app-prod")
        state.record_connection("app-prod", "/srv/app")
        state.clear_history()
        self.assertTrue(state.is_favorite("app-prod"))
        self.assertEqual(state.recents, {})
        self.assertIsNone(state.last_path("app-prod"))


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
            exit_code = ssh_home.run(["--list", "--no-state", "--config", str(config_path)])
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
                        "--no-state",
                    ]
                )

        self.assertEqual(exit_code, 7)
        self.assertEqual(len(instances), 1)
        self.assertIn("/srv/app", instances[0].remote_script)
        self.assertTrue(instances[0].closed)

    def test_clear_history_flag_preserves_favorites(self) -> None:
        config_path = REPO_ROOT / "tests" / "fixtures" / "ssh_config"
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state = ssh_home.SSHHomeState.load(state_path)
            state.toggle_favorite("app-prod")
            state.record_connection("app-prod", "/srv/app")
            state.save()

            with mock.patch("sys.stdout", new=io.StringIO()):
                exit_code = ssh_home.run(
                    [
                        "--list",
                        "--config",
                        str(config_path),
                        "--state-file",
                        str(state_path),
                        "--clear-history",
                    ]
                )

            reloaded = ssh_home.SSHHomeState.load(state_path)
            self.assertEqual(exit_code, 0)
            self.assertTrue(reloaded.is_favorite("app-prod"))
            self.assertEqual(reloaded.recents, {})
            self.assertIsNone(reloaded.last_path("app-prod"))


class TUIHelperTests(unittest.TestCase):
    def test_is_enter_accepts_newline_string(self) -> None:
        ui = object.__new__(ssh_home.SSHHomeTUI)
        self.assertTrue(ui._is_enter("\n"))
        self.assertTrue(ui._is_enter("\r"))

    def test_layout_for_size_uses_responsive_breakpoints(self) -> None:
        full = ssh_home.layout_for_size(24, 120)
        stacked = ssh_home.layout_for_size(24, 90)
        minimal = ssh_home.layout_for_size(16, 60)

        self.assertEqual(full.mode, "full")
        self.assertTrue(full.show_panel)
        self.assertTrue(full.show_graph)
        self.assertEqual(full.panel_position, "side")
        self.assertLessEqual(full.list_width, 52)
        self.assertLess(full.panel_x + full.panel_width, full.width)
        self.assertEqual(stacked.mode, "stacked")
        self.assertTrue(stacked.show_panel)
        self.assertEqual(stacked.panel_position, "stack")
        self.assertEqual(stacked.panel_x, 0)
        self.assertEqual(stacked.panel_width, stacked.width - 1)
        self.assertLess(stacked.list_start, stacked.panel_y)
        self.assertFalse(stacked.show_graph)
        self.assertEqual(minimal.mode, "minimal")
        self.assertFalse(minimal.show_panel)
        self.assertEqual(minimal.panel_position, "none")

    def test_truncate_middle_keeps_edges_inside_width(self) -> None:
        value = ssh_home.truncate_middle("/srv/app/releases/current", 12)
        self.assertEqual(len(value), 12)
        self.assertTrue(value.startswith("/srv"))
        self.assertTrue(value.endswith("rrent"))

    def test_make_bar_fits_requested_width(self) -> None:
        bar = ssh_home.make_bar("fav", 2, 4, 18)
        self.assertEqual(len(bar), 18)
        self.assertIn("[", bar)
        self.assertIn("]", bar)
        self.assertIn("2", bar)

    def test_host_group_counts_splits_favorites_recents_and_other(self) -> None:
        state = ssh_home.SSHHomeState(path=None, enabled=True)
        state.toggle_favorite("app-prod")
        state.data["recents"] = {"app-prod": 20.0, "gateway": 10.0}

        counts = ssh_home.host_group_counts(["app-prod", "gateway", "nested-a"], state)

        self.assertEqual(counts, {"favorites": 1, "recent": 1, "other": 1})

    def test_init_curses_palette_noops_without_color_support(self) -> None:
        class NoColorCurses:
            @staticmethod
            def has_colors() -> bool:
                return False

            @staticmethod
            def start_color() -> None:
                raise AssertionError("start_color should not be called")

        palette = ssh_home.init_curses_palette(NoColorCurses)

        self.assertEqual(palette, {name: 0 for name in ssh_home.TUI_COLOR_PAIR_IDS})

    def test_restore_terminal_for_shell_writes_mouse_reset_only_to_tty(self) -> None:
        tty = FakeTTY("")
        pipe = PromptPipe("")

        ssh_home.restore_terminal_for_shell(tty)
        ssh_home.restore_terminal_for_shell(pipe)

        written = "".join(tty.writes)
        self.assertIn("\x1b[?1000l", written)
        self.assertIn("\x1b[?1006l", written)
        self.assertIn("\x1b[?1049l", written)
        self.assertEqual(pipe.getvalue(), "")


class PublicSafetyTests(unittest.TestCase):
    def test_repo_files_do_not_include_private_markers(self) -> None:
        forbidden = [
            "/Users/" + "santiago",
            "192." + "168.",
            "100." + "111.",
            "OPEN" + "ROUTER",
            "MINI" + "MAX",
            "API" + "_KEY",
            "TOK" + "EN",
            "SEC" + "RET",
            "prox" + "mox",
            "her" + "mes",
            "n" + "8n",
        ]
        for path in REPO_ROOT.rglob("*"):
            if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in forbidden:
                self.assertNotIn(marker, text, f"{marker!r} leaked in {path}")


if __name__ == "__main__":
    unittest.main()
