#!/usr/bin/env python3
"""Interactive SSH host picker and remote directory navigator."""

from __future__ import annotations

import argparse
import glob
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TextIO


LIST_SPLIT_MARKER = "__SSH_HOME_DIRS__"
CONTROL_PERSIST_SECONDS = 600
STATE_VERSION = 1
HOST_VIEW_ALL = "all"
HOST_VIEW_FAVORITES = "favorites"
HOST_VIEW_RECENTS = "recents"
HOST_VIEWS = (HOST_VIEW_ALL, HOST_VIEW_FAVORITES, HOST_VIEW_RECENTS)
TUI_COLOR_PAIR_IDS = {
    "brand": 1,
    "accent": 2,
    "favorite": 3,
    "muted": 4,
    "error": 5,
}


class SSHHomeError(Exception):
    """Raised when the CLI cannot continue."""


class ChangeHostRequested(Exception):
    """Raised when the TUI wants to go back to host selection."""


@dataclass
class ResolvedHost:
    alias: str
    hostname: str
    user: str
    port: str
    proxyjump: str


class PromptIO:
    """Reads prompts from a TTY even when stdin is piped."""

    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        tty_factory: Callable[[], TextIO] | None = None,
    ) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._tty_factory = tty_factory or self._open_tty

        stdin_is_tty = bool(getattr(self.stdin, "isatty", lambda: False)())
        stdout_is_tty = bool(getattr(self.stdout, "isatty", lambda: False)())

        if stdin_is_tty and stdout_is_tty:
            self.read_stream = self.stdin
            self.write_stream = self.stdout
        else:
            tty_stream = self._tty_factory()
            self.read_stream = tty_stream
            self.write_stream = tty_stream

    @staticmethod
    def _open_tty() -> TextIO:
        try:
            return open("/dev/tty", "r+", encoding="utf-8", buffering=1)
        except OSError as exc:
            raise SSHHomeError(
                "No pude abrir /dev/tty para el modo interactivo. "
                "Ejecuta el script desde una terminal real."
            ) from exc

    def println(self, message: str = "") -> None:
        self.write_stream.write(f"{message}\n")
        self.write_stream.flush()

    def prompt(self, message: str) -> str:
        self.write_stream.write(message)
        self.write_stream.flush()
        value = self.read_stream.readline()
        if value == "":
            raise SSHHomeError("La entrada interactiva terminó inesperadamente.")
        return value.rstrip("\n")


class EffectiveSSHConfig:
    """Flattens Include directives into a temporary config file for ssh -F."""

    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ssh-home-config-", dir="/tmp")
        self.path = Path(self.temp_dir.name) / "config"

    def __enter__(self) -> Path:
        parts: list[str] = []
        for file_path in iter_ssh_config_files(self.source_path):
            parts.append(f"# Source: {file_path}\n")
            for raw_line in file_path.read_text(encoding="utf-8").splitlines():
                tokens = parse_config_line(raw_line)
                if tokens and tokens[0].lower() == "include":
                    continue
                parts.append(f"{raw_line}\n")
        self.path.write_text("".join(parts), encoding="utf-8")
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        self.temp_dir.cleanup()


@dataclass
class TUISelection:
    host: str
    path: str
    session: "SSHMasterSession"


@dataclass(frozen=True)
class TUILayout:
    mode: str
    height: int
    width: int
    header_rows: int
    list_start: int
    list_width: int
    panel_x: int
    panel_width: int
    show_panel: bool
    show_graph: bool
    show_logo: bool


def layout_for_size(height: int, width: int) -> TUILayout:
    safe_width = max(1, width)
    safe_height = max(1, height)

    if safe_width >= 100 and safe_height >= 22:
        list_width = max(34, min(int(safe_width * 0.56), safe_width - 36))
        panel_x = list_width + 2
        panel_width = max(0, safe_width - panel_x - 1)
        return TUILayout(
            mode="full",
            height=safe_height,
            width=safe_width,
            header_rows=5,
            list_start=7,
            list_width=list_width,
            panel_x=panel_x,
            panel_width=panel_width,
            show_panel=panel_width >= 28,
            show_graph=panel_width >= 30,
            show_logo=True,
        )

    if safe_width >= 72 and safe_height >= 18:
        panel_width = 26 if safe_width >= 86 else 0
        panel_x = max(0, safe_width - panel_width - 1) if panel_width else safe_width
        list_width = panel_x - 2 if panel_width else safe_width - 1
        return TUILayout(
            mode="compact",
            height=safe_height,
            width=safe_width,
            header_rows=4,
            list_start=6,
            list_width=max(28, list_width),
            panel_x=panel_x,
            panel_width=panel_width,
            show_panel=panel_width >= 24,
            show_graph=False,
            show_logo=safe_width >= 86,
        )

    return TUILayout(
        mode="minimal",
        height=safe_height,
        width=safe_width,
        header_rows=3,
        list_start=4,
        list_width=safe_width - 1,
        panel_x=safe_width,
        panel_width=0,
        show_panel=False,
        show_graph=False,
        show_logo=False,
    )


def truncate_middle(value: object, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    left = (width - 3) // 2
    right = width - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def make_bar(label: str, value: int, total: int, width: int) -> str:
    if width <= 0:
        return ""
    short_label = truncate_middle(label, 6).ljust(min(6, max(3, len(label))))
    prefix = f"{short_label} "
    suffix = f" {max(0, value)}"
    bar_width = width - len(prefix) - len(suffix) - 2
    if bar_width < 4:
        return truncate_middle(f"{label} {value}", width)
    ratio = 0 if total <= 0 else max(0.0, min(1.0, value / total))
    filled = int(round(bar_width * ratio))
    bar = "#" * filled + "-" * (bar_width - filled)
    return truncate_middle(f"{prefix}[{bar}]{suffix}", width)


def host_group_counts(hosts: list[str], state: "SSHHomeState") -> dict[str, int]:
    favorites = set(favorite_hosts(hosts, state))
    recents = {host for host in recent_hosts(hosts, state) if host not in favorites}
    other_count = max(0, len(hosts) - len(favorites) - len(recents))
    return {
        "favorites": len(favorites),
        "recent": len(recents),
        "other": other_count,
    }


def init_curses_palette(curses_mod) -> dict[str, int]:
    palette = {name: 0 for name in TUI_COLOR_PAIR_IDS}
    try:
        if not curses_mod.has_colors():
            return palette
        curses_mod.start_color()
        if hasattr(curses_mod, "use_default_colors"):
            curses_mod.use_default_colors()
        pair_defs = {
            "brand": (curses_mod.COLOR_CYAN, -1),
            "accent": (curses_mod.COLOR_GREEN, -1),
            "favorite": (curses_mod.COLOR_YELLOW, -1),
            "muted": (curses_mod.COLOR_BLUE, -1),
            "error": (curses_mod.COLOR_RED, -1),
        }
        for name, (foreground, background) in pair_defs.items():
            pair_id = TUI_COLOR_PAIR_IDS[name]
            curses_mod.init_pair(pair_id, foreground, background)
            palette[name] = curses_mod.color_pair(pair_id)
    except Exception:
        return {name: 0 for name in TUI_COLOR_PAIR_IDS}
    return palette


def default_state_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path("~/.config").expanduser()
    return base / "ssh-home" / "state.json"


def empty_state_data() -> dict:
    return {
        "version": STATE_VERSION,
        "favorites": [],
        "recents": {},
        "last_paths": {},
        "preferences": {"view": HOST_VIEW_ALL},
    }


def normalize_state_data(raw: object) -> dict:
    data = empty_state_data()
    if not isinstance(raw, dict):
        return data

    favorites = raw.get("favorites", [])
    if isinstance(favorites, list):
        data["favorites"] = unique_preserving_order(
            item for item in favorites if isinstance(item, str) and item
        )

    recents = raw.get("recents", {})
    if isinstance(recents, dict):
        data["recents"] = {
            key: float(value)
            for key, value in recents.items()
            if isinstance(key, str) and key and isinstance(value, (int, float))
        }

    last_paths = raw.get("last_paths", {})
    if isinstance(last_paths, dict):
        data["last_paths"] = {
            key: value
            for key, value in last_paths.items()
            if isinstance(key, str) and key and isinstance(value, str) and value
        }

    preferences = raw.get("preferences", {})
    if isinstance(preferences, dict):
        view = preferences.get("view", HOST_VIEW_ALL)
        data["preferences"]["view"] = view if view in HOST_VIEWS else HOST_VIEW_ALL

    return data


class SSHHomeState:
    def __init__(self, path: Path | None, enabled: bool = True, data: dict | None = None) -> None:
        self.path = path
        self.enabled = enabled
        self.data = normalize_state_data(data or {})

    @classmethod
    def disabled(cls) -> "SSHHomeState":
        return cls(path=None, enabled=False, data=empty_state_data())

    @classmethod
    def load(cls, path: Path) -> "SSHHomeState":
        expanded = Path(os.path.expanduser(os.path.expandvars(str(path))))
        raw: object = {}
        if expanded.exists():
            try:
                raw = json.loads(expanded.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
        state = cls(path=expanded, enabled=True, data=normalize_state_data(raw))
        state.save()
        return state

    @property
    def favorites(self) -> list[str]:
        return list(self.data["favorites"])

    @property
    def recents(self) -> dict[str, float]:
        return dict(self.data["recents"])

    def preference(self, key: str, default: str) -> str:
        value = self.data["preferences"].get(key, default)
        return value if isinstance(value, str) else default

    def set_preference(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        self.data["preferences"][key] = value

    def is_favorite(self, host: str) -> bool:
        return host in self.data["favorites"]

    def toggle_favorite(self, host: str) -> bool:
        if not self.enabled:
            return False
        favorites = self.data["favorites"]
        if host in favorites:
            favorites.remove(host)
            return False
        favorites.append(host)
        return True

    def last_path(self, host: str) -> str | None:
        value = self.data["last_paths"].get(host)
        return value if isinstance(value, str) and value else None

    def record_connection(self, host: str, path: str) -> None:
        if not self.enabled:
            return
        self.data["recents"][host] = time.time()
        self.data["last_paths"][host] = path

    def clear_history(self) -> None:
        if not self.enabled:
            return
        self.data["recents"] = {}
        self.data["last_paths"] = {}

    def save(self) -> None:
        if not self.enabled or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def parse_config_line(raw_line: str) -> list[str]:
    try:
        return shlex.split(raw_line, comments=True, posix=True)
    except ValueError:
        return raw_line.split("#", 1)[0].split()


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def is_selectable_host(token: str) -> bool:
    return bool(token) and not any(char in token for char in ("*", "?", "!"))


def filter_candidates(items: list[str], query: str) -> list[str]:
    if not query:
        return items
    lowered = query.casefold()
    return [item for item in items if lowered in item.casefold()]


def recent_hosts(hosts: list[str], state: SSHHomeState) -> list[str]:
    known_hosts = set(hosts)
    return [
        host
        for host, _timestamp in sorted(
            state.recents.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        if host in known_hosts
    ]


def favorite_hosts(hosts: list[str], state: SSHHomeState) -> list[str]:
    known_hosts = set(hosts)
    return [host for host in state.favorites if host in known_hosts]


def ordered_hosts(hosts: list[str], state: SSHHomeState, view: str = HOST_VIEW_ALL) -> list[str]:
    favorites = favorite_hosts(hosts, state)
    recents = recent_hosts(hosts, state)
    if view == HOST_VIEW_FAVORITES:
        return favorites
    if view == HOST_VIEW_RECENTS:
        return recents

    pinned = set(favorites) | set(recents)
    rest = sorted((host for host in hosts if host not in pinned), key=str.casefold)
    return favorites + [host for host in recents if host not in favorites] + rest


def host_group(host: str, state: SSHHomeState) -> str:
    if state.is_favorite(host):
        return "favorite"
    if host in state.recents:
        return "recent"
    return "host"


def next_host_view(view: str) -> str:
    if view not in HOST_VIEWS:
        return HOST_VIEW_ALL
    index = HOST_VIEWS.index(view)
    return HOST_VIEWS[(index + 1) % len(HOST_VIEWS)]


def host_view_label(view: str) -> str:
    return {
        HOST_VIEW_ALL: "ALL",
        HOST_VIEW_FAVORITES: "FAV",
        HOST_VIEW_RECENTS: "RECENT",
    }.get(view, "ALL")


def breadcrumb_path(path: str, max_parts: int = 4) -> str:
    normalized = posixpath.normpath(path or "/")
    if normalized == "/":
        return "/"
    parts = [part for part in normalized.split("/") if part]
    if len(parts) <= max_parts:
        return "/" + " / ".join(parts)
    return "... / " + " / ".join(parts[-max_parts:])


def iter_ssh_config_files(config_path: Path) -> list[Path]:
    visited: set[Path] = set()
    ordered: list[Path] = []

    def walk(path: Path) -> None:
        expanded = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
        if expanded in visited or not expanded.is_file():
            return
        visited.add(expanded)
        ordered.append(expanded)

        for raw_line in expanded.read_text(encoding="utf-8").splitlines():
            parts = parse_config_line(raw_line)
            if not parts:
                continue
            keyword = parts[0].lower()
            if keyword != "include" or len(parts) < 2:
                continue
            for include_pattern in parts[1:]:
                include_path = Path(os.path.expanduser(os.path.expandvars(include_pattern)))
                if not include_path.is_absolute():
                    include_path = expanded.parent / include_path
                for match in sorted(glob.glob(str(include_path))):
                    walk(Path(match))

    walk(config_path)
    return ordered


def parse_hosts_from_configs(config_path: Path) -> list[str]:
    hosts: list[str] = []
    for file_path in iter_ssh_config_files(config_path):
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            parts = parse_config_line(raw_line)
            if not parts or parts[0].lower() != "host":
                continue
            for token in parts[1:]:
                if is_selectable_host(token):
                    hosts.append(token)
    return unique_preserving_order(hosts)


def _resolve_host_from_effective_config(alias: str, config_path: Path) -> ResolvedHost:
    cmd = ["ssh", "-G", "-F", str(config_path), alias]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(config_path.parent),
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "ssh -G falló"
        raise SSHHomeError(f"No pude resolver `{alias}`: {message}")

    values: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split(None, 1)
        if len(parts) == 2:
            key, value = parts
            values[key.lower()] = value.strip()

    return ResolvedHost(
        alias=alias,
        hostname=values.get("hostname", ""),
        user=values.get("user", ""),
        port=values.get("port", ""),
        proxyjump=values.get("proxyjump", ""),
    )


def resolve_host(alias: str, config_path: Path) -> ResolvedHost:
    with EffectiveSSHConfig(config_path) as effective_config:
        return _resolve_host_from_effective_config(alias, effective_config)


class SSHMasterSession:
    """Owns a temporary SSH control socket."""

    def __init__(self, alias: str, config_path: Path) -> None:
        self.alias = alias
        self.config_path = config_path
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ssh-home-", dir="/tmp")
        self.control_path = os.path.join(self.temp_dir.name, "control.sock")
        self.established = False

    def _base_cmd(self) -> list[str]:
        return [
            "ssh",
            "-F",
            str(self.config_path),
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        ]

    def establish(self) -> None:
        if self.established:
            return
        cmd = self._base_cmd() + [
            "-o",
            "ControlMaster=yes",
            "-f",
            "-N",
            self.alias,
        ]
        result = subprocess.run(cmd, check=False, cwd=str(self.config_path.parent))
        if result.returncode != 0:
            raise SSHHomeError(
                f"No pude abrir la conexión maestra para `{self.alias}` "
                f"(exit {result.returncode})."
            )

        deadline = time.time() + 3
        while time.time() < deadline:
            if os.path.exists(self.control_path):
                self.established = True
                return
            time.sleep(0.05)
        raise SSHHomeError(
            f"SSH no creó el socket de control para `{self.alias}` en `{self.control_path}`."
        )

    def run_capture(self, remote_script: str) -> subprocess.CompletedProcess[str]:
        self.establish()
        cmd = self._base_cmd() + [
            "-o",
            "ControlMaster=no",
            self.alias,
            f"sh -lc {shlex.quote(remote_script)}",
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(self.config_path.parent),
        )

    def run_interactive(self, remote_script: str) -> int:
        self.establish()
        cmd = self._base_cmd() + [
            "-o",
            "ControlMaster=no",
            "-t",
            self.alias,
            f"sh -lc {shlex.quote(remote_script)}",
        ]
        return subprocess.call(cmd, cwd=str(self.config_path.parent))

    def close(self) -> None:
        try:
            if self.established:
                subprocess.run(
                    [
                        "ssh",
                        "-F",
                        str(self.config_path),
                        "-S",
                        self.control_path,
                        "-O",
                        "exit",
                        self.alias,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=str(self.config_path.parent),
                )
        finally:
            self.temp_dir.cleanup()


def build_listing_script(path: str | None) -> str:
    lines = ["set -eu"]
    if path:
        lines.append(f"cd -- {shlex.quote(path)}")
    lines.extend(
        [
            "pwd",
            f"printf '%s\\n' {shlex.quote(LIST_SPLIT_MARKER)}",
            "find . -mindepth 1 -maxdepth 1 -type d -print | sed 's#^\\./##' | LC_ALL=C sort",
        ]
    )
    return "\n".join(lines)


def list_remote_directories(
    session: SSHMasterSession, path: str | None
) -> tuple[str, list[str]]:
    result = session.run_capture(build_listing_script(path))
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "No pude listar el directorio remoto."
        raise SSHHomeError(message)

    lines = result.stdout.splitlines()
    if LIST_SPLIT_MARKER not in lines:
        raise SSHHomeError("La respuesta remota no tuvo el formato esperado.")
    marker_index = lines.index(LIST_SPLIT_MARKER)
    current_path = lines[0].strip() if marker_index > 0 else ""
    directories = [line.strip() for line in lines[marker_index + 1 :] if line.strip()]
    return current_path, directories


class SSHHomeTUI:
    """Curses-based host picker and remote directory browser."""

    ENTER_KEYS = {10, 13}
    BACKSPACE_KEYS = {8, 127}
    ENTER_CHARS = {"\n", "\r"}

    def __init__(
        self,
        stdscr,
        hosts: list[str],
        config_path: Path,
        state: SSHHomeState,
        initial_host: str | None = None,
        initial_path: str | None = None,
    ) -> None:
        self.stdscr = stdscr
        self.hosts = hosts
        self.config_path = config_path
        self.state = state
        self.initial_host = initial_host
        self.initial_path = initial_path
        self.host_query = ""
        self.host_index = 0
        self.dir_query = ""
        self.dir_index = 0
        self.host_view = self.state.preference("view", HOST_VIEW_ALL)
        self.selected_start_path: str | None = None
        self.show_help = False
        self.status_message = "Ready"
        self.resolved_cache: dict[str, ResolvedHost] = {}
        self.palette = init_curses_palette(self._curses)

    def run(self) -> TUISelection:
        if self.initial_host:
            host = self.initial_host
            pending_path = self.initial_path
        else:
            host = self._select_host()
            pending_path = self.selected_start_path
        while True:
            try:
                return self._browse_host(host, pending_path)
            except ChangeHostRequested:
                host = self._select_host()
                pending_path = self.selected_start_path

    def _select_host(self) -> str:
        self.selected_start_path = None
        while True:
            ordered = ordered_hosts(self.hosts, self.state, self.host_view)
            filtered = filter_candidates(ordered, self.host_query)
            self.host_index = self._clamp_index(self.host_index, len(filtered))
            selected = filtered[self.host_index] if filtered else None
            self._render_host_screen(filtered, selected)
            key = self.stdscr.get_wch()

            if key in ("q", "Q"):
                raise SSHHomeError("Conexión cancelada por el usuario.")
            if key == "?":
                self.show_help = not self.show_help
                continue
            if key in ("\t",) and not self.host_query:
                self.host_view = next_host_view(self.host_view)
                self.state.set_preference("view", self.host_view)
                self.state.save()
                self.host_index = 0
                self.status_message = f"View: {host_view_label(self.host_view)}"
                continue
            if key in ("\t",):
                self.host_query = ""
                self.host_index = 0
                continue
            if not self.host_query and key in ("a", "A"):
                self.host_view = HOST_VIEW_ALL
                self.state.set_preference("view", self.host_view)
                self.state.save()
                self.host_index = 0
                self.status_message = "Showing all hosts"
                continue
            if not self.host_query and key in ("r", "R"):
                self.host_view = HOST_VIEW_RECENTS
                self.state.set_preference("view", self.host_view)
                self.state.save()
                self.host_index = 0
                self.status_message = "Showing recent hosts"
                continue
            if not self.host_query and key in ("f", "F"):
                if selected is not None:
                    if not self.state.enabled:
                        self.status_message = "State disabled (--no-state)"
                        continue
                    is_favorite = self.state.toggle_favorite(selected)
                    self.state.save()
                    self.status_message = (
                        f"Favorited {selected}" if is_favorite else f"Unfavorited {selected}"
                    )
                continue
            if not self.host_query and key in ("l", "L"):
                if selected is None:
                    continue
                last_path = self.state.last_path(selected)
                if last_path:
                    self.selected_start_path = last_path
                    self.dir_query = ""
                    self.dir_index = 0
                    return selected
                self.status_message = f"No last path for {selected}"
                continue
            if self._is_enter(key):
                if selected is not None:
                    self.selected_start_path = None
                    self.dir_query = ""
                    self.dir_index = 0
                    return selected
                continue
            if key == self._curses.KEY_UP:
                self.host_index = max(0, self.host_index - 1)
                continue
            if key == self._curses.KEY_DOWN:
                self.host_index = min(max(len(filtered) - 1, 0), self.host_index + 1)
                continue
            if self._is_backspace(key):
                if self.host_query:
                    self.host_query = self.host_query[:-1]
                    self.host_index = 0
                continue
            if isinstance(key, str) and key.isprintable():
                self.host_query += key
                self.host_index = 0

    def _browse_host(self, host: str, start_path: str | None) -> TUISelection:
        session = SSHMasterSession(host, self.config_path)
        keep_session = False
        try:
            self.status_message = f"Connecting to {host}..."
            self._run_in_shell_mode(session.establish)
            self.status_message = "Loading remote directories..."
            current_path, directories = list_remote_directories(session, start_path)
            self.dir_query = ""
            self.dir_index = 0
            self.status_message = f"Connected to {host}"

            while True:
                filtered = filter_candidates(directories, self.dir_query)
                entries = self._directory_entries(current_path, filtered)
                self.dir_index = self._clamp_index(self.dir_index, len(entries))
                self._render_directory_screen(host, current_path, entries)
                key = self.stdscr.get_wch()

                if key in ("q", "Q"):
                    raise SSHHomeError("Conexión cancelada por el usuario.")
                if key == "?":
                    self.show_help = not self.show_help
                    continue
                if key in ("\t", "h", "H"):
                    raise ChangeHostRequested()
                if key in ("f", "F") and not self.dir_query:
                    if not self.state.enabled:
                        self.status_message = "State disabled (--no-state)"
                        continue
                    is_favorite = self.state.toggle_favorite(host)
                    self.state.save()
                    self.status_message = (
                        f"Favorited {host}" if is_favorite else f"Unfavorited {host}"
                    )
                    continue
                if key in ("r", "R") and not self.dir_query:
                    self.status_message = "Refreshing remote directory..."
                    current_path, directories = list_remote_directories(session, current_path)
                    self.dir_index = 0
                    self.status_message = "Directory refreshed"
                    continue
                if key in ("l", "L") and not self.dir_query:
                    last_path = self.state.last_path(host)
                    if last_path:
                        current_path, directories = list_remote_directories(session, last_path)
                        self.dir_query = ""
                        self.dir_index = 0
                        self.status_message = f"Jumped to last path: {last_path}"
                    else:
                        self.status_message = f"No last path for {host}"
                    continue
                if key == self._curses.KEY_LEFT:
                    current_path, directories = list_remote_directories(
                        session,
                        posixpath.dirname(current_path.rstrip("/")) or "/",
                    )
                    self.dir_query = ""
                    self.dir_index = 0
                    self.status_message = "Moved up one level"
                    continue
                if key in ("/", "m", "M"):
                    manual = self._prompt_line(
                        "Ruta remota (absoluta o relativa): ",
                        current_path,
                    )
                    if manual is None:
                        continue
                    candidate = manual if manual.startswith("/") else posixpath.normpath(
                        posixpath.join(current_path, manual)
                    )
                    current_path, directories = list_remote_directories(session, candidate)
                    self.dir_query = ""
                    self.dir_index = 0
                    self.status_message = f"Opened {candidate}"
                    continue
                if key == self._curses.KEY_UP:
                    self.dir_index = max(0, self.dir_index - 1)
                    continue
                if key == self._curses.KEY_DOWN:
                    self.dir_index = min(max(len(entries) - 1, 0), self.dir_index + 1)
                    continue
                if self._is_backspace(key):
                    if self.dir_query:
                        self.dir_query = self.dir_query[:-1]
                        self.dir_index = 0
                    else:
                        current_path, directories = list_remote_directories(
                            session,
                            posixpath.dirname(current_path.rstrip("/")) or "/",
                        )
                        self.dir_index = 0
                    continue
                if self._is_enter(key):
                    entry_type, value = entries[self.dir_index]
                    if entry_type == "use":
                        keep_session = True
                        return TUISelection(host=host, path=current_path, session=session)
                    if entry_type == "parent":
                        current_path, directories = list_remote_directories(
                            session,
                            posixpath.dirname(current_path.rstrip("/")) or "/",
                        )
                        self.dir_query = ""
                        self.dir_index = 0
                        self.status_message = "Moved up one level"
                        continue
                    if entry_type == "manual":
                        manual = self._prompt_line(
                            "Ruta remota (absoluta o relativa): ",
                            current_path,
                        )
                        if manual is None:
                            continue
                        candidate = manual if manual.startswith("/") else posixpath.normpath(
                            posixpath.join(current_path, manual)
                        )
                        current_path, directories = list_remote_directories(session, candidate)
                        self.dir_query = ""
                        self.dir_index = 0
                        self.status_message = f"Opened {candidate}"
                        continue
                    if entry_type == "hosts":
                        raise ChangeHostRequested()
                    next_path = posixpath.normpath(posixpath.join(current_path, value))
                    current_path, directories = list_remote_directories(session, next_path)
                    self.dir_query = ""
                    self.dir_index = 0
                    self.status_message = f"Opened {next_path}"
                    continue
                if isinstance(key, str) and key.isprintable():
                    self.dir_query += key
                    self.dir_index = 0
        finally:
            if not keep_session:
                session.close()

    @property
    def _curses(self):
        import curses

        return curses

    def _run_in_shell_mode(self, callback: Callable[[], None]) -> None:
        curses = self._curses
        curses.def_prog_mode()
        curses.endwin()
        try:
            callback()
        finally:
            curses.reset_prog_mode()
            self.stdscr.clear()
            self.stdscr.refresh()

    def _prompt_line(self, label: str, initial: str = "") -> str | None:
        curses = self._curses
        current = initial
        while True:
            self._draw_prompt(label, current)
            key = self.stdscr.get_wch()
            if key == "\x1b":
                return None
            if self._is_enter(key):
                return current.strip()
            if self._is_backspace(key):
                current = current[:-1]
                continue
            if isinstance(key, str) and key.isprintable():
                current += key

    def _draw_prompt(self, label: str, current: str) -> None:
        height, width = self.stdscr.getmaxyx()
        if height < 2:
            return
        self.stdscr.move(height - 2, 0)
        self.stdscr.clrtoeol()
        self._draw(height - 2, 0, label, width - 1, self._attr("brand", bold=True))
        self.stdscr.move(height - 1, 0)
        self.stdscr.clrtoeol()
        self._draw(height - 1, 0, current, width - 1)
        self.stdscr.refresh()

    def _attr(
        self,
        color: str | None = None,
        *,
        bold: bool = False,
        dim: bool = False,
        reverse: bool = False,
    ) -> int:
        curses = self._curses
        attr = self.palette.get(color or "", 0)
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        if reverse:
            attr |= curses.A_REVERSE
        return attr

    def _draw(self, y: int, x: int, text: object, width: int, attr: int = 0) -> None:
        screen_height, screen_width = self.stdscr.getmaxyx()
        if y < 0 or y >= screen_height or x < 0 or x >= screen_width:
            return
        safe_width = min(max(0, width), screen_width - x)
        if safe_width <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, truncate_middle(text, safe_width), safe_width, attr)
        except Exception:
            # Curses can reject writes at the bottom-right cell on some terminals.
            return

    def _render_header(self, layout: TUILayout, context: str, detail: str) -> None:
        if layout.mode == "minimal":
            self._draw(
                0,
                0,
                f"ssh> {context}",
                layout.width - 1,
                self._attr("brand", bold=True),
            )
            self._draw(1, 0, detail, layout.width - 1, self._attr("muted", dim=True))
            return

        self._draw(
            0,
            0,
            "ssh-home :: project by srdize3322",
            layout.width - 1,
            self._attr("brand", bold=True),
        )
        badge = "ssh://home"
        if layout.show_logo:
            self._draw(
                0,
                max(0, layout.width - len(badge) - 1),
                badge,
                len(badge),
                self._attr("accent", bold=True),
            )
        self._draw(1, 0, context, layout.width - 1, self._attr("accent"))
        self._draw(2, 0, detail, layout.width - 1, self._attr("muted", dim=True))

    def _render_host_panel(
        self,
        layout: TUILayout,
        selected: str,
        counts: dict[str, int],
    ) -> None:
        if not layout.show_panel:
            return

        resolved = self._resolved_host(selected)
        x = layout.panel_x
        width = layout.panel_width
        row = 4 if layout.mode == "full" else 5
        self._draw(row, x, "SSH NODE", width, self._attr("brand", bold=True))
        self._draw(row + 1, x, "ssh://home", width, self._attr("accent"))
        self._draw(row + 2, x, "project by srdize3322", width, self._attr("muted", dim=True))

        details = [
            ("Alias", resolved.alias),
            ("User", resolved.user or "-"),
            ("HostName", resolved.hostname or "-"),
            ("Port", resolved.port or "-"),
            ("ProxyJump", resolved.proxyjump or "-"),
            ("Favorite", "yes" if self.state.is_favorite(selected) else "no"),
            ("Last path", self.state.last_path(selected) or "-"),
        ]
        detail_row = row + 4
        for offset, (label, value) in enumerate(details):
            if detail_row + offset >= layout.height - 2:
                break
            available = max(1, width - len(label) - 2)
            self._draw(
                detail_row + offset,
                x,
                f"{label:<9} {truncate_middle(value, available)}",
                width,
            )

        if not layout.show_graph:
            return

        graph_row = detail_row + len(details) + 2
        if graph_row + 4 >= layout.height - 2:
            return
        total = sum(counts.values())
        self._draw(graph_row, x, "HOST MIX", width, self._attr("favorite", bold=True))
        self._draw(graph_row + 1, x, make_bar("fav", counts["favorites"], total, width), width)
        self._draw(graph_row + 2, x, make_bar("recent", counts["recent"], total, width), width)
        self._draw(graph_row + 3, x, make_bar("other", counts["other"], total, width), width)

    def _render_directory_panel(self, layout: TUILayout, host: str, current_path: str) -> None:
        if not layout.show_panel:
            return
        x = layout.panel_x
        width = layout.panel_width
        row = 4 if layout.mode == "full" else 5
        depth = len([part for part in current_path.split("/") if part])
        self._draw(row, x, "SESSION", width, self._attr("brand", bold=True))
        self._draw(row + 1, x, "ssh://home", width, self._attr("accent"))
        self._draw(row + 2, x, f"host {host}", width)
        self._draw(row + 3, x, f"path {breadcrumb_path(current_path)}", width)
        if layout.show_graph and row + 6 < layout.height - 2:
            self._draw(row + 5, x, "PATH DEPTH", width, self._attr("favorite", bold=True))
            self._draw(row + 6, x, make_bar("depth", min(depth, 12), 12, width), width)

    def _render_host_screen(self, hosts: list[str], selected: str | None) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        layout = layout_for_size(height, width)
        counts = host_group_counts(self.hosts, self.state)
        context = (
            f"HOMELAB PRO [{host_view_label(self.host_view)}] | "
            f"{len(hosts)}/{len(self.hosts)} visible"
        )
        detail = (
            f"hosts {len(self.hosts)} | fav {counts['favorites']} | "
            f"recent {counts['recent']} | filter {self.host_query or 'none'}"
        )
        self._render_header(layout, context, detail)

        filter_row = 3 if layout.mode == "minimal" else 4
        self._draw(
            filter_row,
            0,
            f"Filter: {self.host_query or 'none'}",
            layout.list_width,
            self._attr("brand", bold=True),
        )
        if layout.mode != "minimal":
            self._draw(
                filter_row + 1,
                0,
                "Hosts",
                layout.list_width,
                self._attr("muted", dim=True),
            )

        if selected is not None:
            self._render_host_panel(layout, selected, counts)

        list_start = layout.list_start
        visible_rows = max(0, height - list_start - 2)
        offset = self._scroll_offset(self.host_index, len(hosts), visible_rows)
        if visible_rows <= 0:
            pass
        elif not hosts:
            self._draw(
                list_start,
                0,
                "No matching SSH hosts. Backspace clears the filter.",
                layout.list_width,
                self._attr("error"),
            )
        else:
            for row, host in enumerate(hosts[offset : offset + visible_rows]):
                index = offset + row
                group = host_group(host, self.state)
                color = {"favorite": "favorite", "recent": "accent", "host": None}[group]
                attr = self._attr(color, reverse=index == self.host_index)
                label = self._label_for_host(host)
                self._draw(list_start + row, 0, label, layout.list_width, attr)

        if self.show_help:
            help_x = layout.panel_x if layout.show_panel else max(0, width - 25)
            help_width = layout.panel_width if layout.show_panel else min(24, width - 1)
            self._render_help_box(
                [
                    "Enter open",
                    "f favorite",
                    "l last path",
                    "r recents",
                    "a all",
                    "Tab view",
                    "q quit",
                ],
                help_x,
                height,
                help_width,
            )

        footer = f"{self.status_message} | ? help | Enter open | f fav | l last | r recent | a all | Tab view | q"
        self._draw(height - 1, 0, footer, width - 1, self._attr("muted", dim=True))
        self.stdscr.refresh()

    def _render_directory_screen(
        self,
        host: str,
        current_path: str,
        entries: list[tuple[str, str]],
    ) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        layout = layout_for_size(height, width)
        context = f"REMOTE BROWSER | host {host}"
        detail = f"path {breadcrumb_path(current_path)} | filter {self.dir_query or 'none'}"
        self._render_header(layout, context, detail)
        self._draw(
            3 if layout.mode == "minimal" else 4,
            0,
            f"Filter: {self.dir_query or 'none'}",
            layout.list_width,
            self._attr("brand", bold=True),
        )
        if layout.mode != "minimal":
            self._draw(
                5,
                0,
                f"Path: {breadcrumb_path(current_path)}",
                layout.list_width,
                self._attr("muted", dim=True),
            )
        self._render_directory_panel(layout, host, current_path)

        list_start = layout.list_start
        visible_rows = max(0, height - list_start - 2)
        offset = self._scroll_offset(self.dir_index, len(entries), visible_rows)
        if visible_rows > 0:
            for row, entry in enumerate(entries[offset : offset + visible_rows]):
                index = offset + row
                entry_type, value = entry
                color = {
                    "use": "accent",
                    "parent": "muted",
                    "manual": "brand",
                    "hosts": "favorite",
                    "dir": None,
                }.get(entry_type)
                attr = self._attr(color, reverse=index == self.dir_index)
                label = self._label_for_directory_entry(entry_type, value)
                self._draw(list_start + row, 0, label, layout.list_width, attr)

        if self.show_help:
            help_x = layout.panel_x if layout.show_panel else max(0, width - 25)
            help_width = layout.panel_width if layout.show_panel else min(24, width - 1)
            self._render_help_box(
                [
                    "Enter open/use",
                    "Left up",
                    "/ manual path",
                    "l last path",
                    "r refresh",
                    "f favorite host",
                    "Tab hosts",
                    "q quit",
                ],
                help_x,
                height,
                help_width,
            )

        footer = (
            f"{self.status_message} | ? help | Enter open/use | Left up | / path | "
            "l last | r refresh | Tab hosts | q"
        )
        self._draw(height - 1, 0, footer, width - 1, self._attr("muted", dim=True))
        self.stdscr.refresh()

    def _render_help_box(self, lines: list[str], x: int, height: int, width: int) -> None:
        if width < 12 or x >= self.stdscr.getmaxyx()[1] - 1:
            return
        start_y = max(3, height - len(lines) - 4)
        self._draw(start_y, x, "HELP :: keys", width, self._attr("brand", bold=True))
        for index, line in enumerate(lines, start=1):
            self._draw(start_y + index, x, line, width)

    def _label_for_host(self, host: str) -> str:
        group = host_group(host, self.state)
        marker = {"favorite": "*", "recent": "~", "host": " "}[group]
        last_path = self.state.last_path(host)
        suffix = f"  {last_path}" if last_path else ""
        return f"{marker} {host}{suffix}"

    def _directory_entries(self, current_path: str, directories: list[str]) -> list[tuple[str, str]]:
        return [
            ("use", current_path),
            ("parent", posixpath.dirname(current_path.rstrip("/")) or "/"),
            ("manual", ""),
            ("hosts", ""),
            *[("dir", directory) for directory in directories],
        ]

    def _label_for_directory_entry(self, entry_type: str, value: str) -> str:
        if entry_type == "use":
            return f"> use current  {value}"
        if entry_type == "parent":
            return "< parent      .."
        if entry_type == "manual":
            return "/ manual path"
        if entry_type == "hosts":
            return "tab host list"
        return f"  {value}"

    def _resolved_host(self, alias: str) -> ResolvedHost:
        if alias not in self.resolved_cache:
            self.resolved_cache[alias] = _resolve_host_from_effective_config(alias, self.config_path)
        return self.resolved_cache[alias]

    @staticmethod
    def _clamp_index(index: int, count: int) -> int:
        if count <= 0:
            return 0
        return min(max(index, 0), count - 1)

    @staticmethod
    def _scroll_offset(index: int, count: int, visible_rows: int) -> int:
        if count <= visible_rows:
            return 0
        half = visible_rows // 2
        return min(max(index - half, 0), count - visible_rows)

    def _is_enter(self, key) -> bool:
        return key in self.ENTER_KEYS or key in self.ENTER_CHARS or key == self._curses.KEY_ENTER

    def _is_backspace(self, key) -> bool:
        return key in self.BACKSPACE_KEYS or key == self._curses.KEY_BACKSPACE


def run_tui(
    hosts: list[str],
    config_path: Path,
    state: SSHHomeState,
    initial_host: str | None = None,
    initial_path: str | None = None,
) -> TUISelection:
    import curses

    def wrapped(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(-1)
        ui = SSHHomeTUI(
            stdscr,
            hosts,
            config_path,
            state,
            initial_host=initial_host,
            initial_path=initial_path,
        )
        return ui.run()

    return curses.wrapper(wrapped)


def choose_host_interactively(hosts: list[str], io: PromptIO) -> str:
    io.println("Hosts SSH detectados:\n")
    for index, host in enumerate(hosts, start=1):
        io.println(f"{index}. {host}")
    while True:
        answer = io.prompt("\nElige un host por número o alias: ").strip()
        if not answer:
            continue
        if answer.lower() == "q":
            raise SSHHomeError("Conexión cancelada por el usuario.")
        if answer.isdigit():
            selected = int(answer)
            if 1 <= selected <= len(hosts):
                return hosts[selected - 1]
        elif answer in hosts:
            return answer
        io.println("Selección inválida. Intenta de nuevo.")


def browse_remote_path(
    session: SSHMasterSession,
    io: PromptIO,
    start_path: str | None = None,
) -> str:
    current_path, directories = list_remote_directories(session, start_path)

    while True:
        io.println("")
        io.println(f"Host: {session.alias}")
        io.println(f"Directorio actual: {current_path}")
        io.println("")
        if directories:
            for index, directory in enumerate(directories, start=1):
                io.println(f"{index}. {directory}")
        else:
            io.println("(sin subdirectorios visibles)")

        io.println("")
        io.println("[c] usar carpeta actual")
        io.println("[u] subir un nivel")
        io.println("[m] escribir ruta manual")
        io.println("[r] refrescar")
        io.println("[q] cancelar")

        answer = io.prompt("> ").strip()
        if not answer:
            continue
        if answer.lower() == "c":
            return current_path
        if answer.lower() == "u":
            parent = posixpath.dirname(current_path.rstrip("/")) or "/"
            current_path, directories = list_remote_directories(session, parent)
            continue
        if answer.lower() == "m":
            manual = io.prompt("Ruta remota (absoluta o relativa): ").strip()
            if not manual:
                continue
            candidate = manual if manual.startswith("/") else posixpath.normpath(
                posixpath.join(current_path, manual)
            )
            current_path, directories = list_remote_directories(session, candidate)
            continue
        if answer.lower() == "r":
            current_path, directories = list_remote_directories(session, current_path)
            continue
        if answer.lower() == "q":
            raise SSHHomeError("Conexión cancelada por el usuario.")
        if answer.isdigit():
            selected = int(answer)
            if 1 <= selected <= len(directories):
                next_path = posixpath.normpath(
                    posixpath.join(current_path, directories[selected - 1])
                )
                current_path, directories = list_remote_directories(session, next_path)
                continue
        io.println("Opción inválida.")


def shell_command_for_path(path: str) -> str:
    return f"cd -- {shlex.quote(path)} && exec ${{SHELL:-/bin/sh}} -l"


def print_host_list(hosts: list[str], config_path: Path, show_resolved: bool) -> int:
    if not hosts:
        print("No encontré hosts utilizables en la config SSH.", file=sys.stderr)
        return 1

    if not show_resolved:
        for host in hosts:
            print(host)
        return 0

    print("alias\thostname\tuser\tport\tproxyjump")
    with EffectiveSSHConfig(config_path) as effective_config:
        for host in hosts:
            resolved = _resolve_host_from_effective_config(host, effective_config)
            print(
                "\t".join(
                    [
                        resolved.alias,
                        resolved.hostname or "-",
                        resolved.user or "-",
                        resolved.port or "-",
                        resolved.proxyjump or "-",
                    ]
                )
            )
    return 0


def can_use_tui() -> bool:
    term = os.environ.get("TERM", "")
    return sys.stdin.isatty() and sys.stdout.isatty() and term not in ("", "dumb")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestor SSH interactivo y portable.")
    parser.add_argument("--list", action="store_true", help="Listar hosts detectados y salir.")
    parser.add_argument("--host", help="Alias SSH a usar directamente.")
    parser.add_argument("--path", help="Ruta remota para saltar el navegador.")
    parser.add_argument(
        "--config",
        default="~/.ssh/config",
        help="Ruta al archivo SSH config a usar (default: ~/.ssh/config).",
    )
    parser.add_argument(
        "--show-resolved",
        action="store_true",
        help="Mostrar HostName/User/Port/ProxyJump resueltos con ssh -G.",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Usar el modo texto simple aunque la TUI esté disponible.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Ruta alternativa para el estado local (default: ~/.config/ssh-home/state.json).",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="No leer ni escribir favoritos, recientes o últimas rutas.",
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Borrar recientes y últimas rutas sin tocar favoritos.",
    )
    args = parser.parse_args(argv)

    state = SSHHomeState.disabled()
    if not args.no_state:
        state_path = Path(args.state_file) if args.state_file else default_state_path()
        state = SSHHomeState.load(state_path)
        if args.clear_history:
            state.clear_history()
            state.save()

    config_path = Path(os.path.expanduser(args.config)).resolve()
    if not config_path.is_file():
        raise SSHHomeError(f"No existe el archivo de config SSH: {config_path}")

    hosts = parse_hosts_from_configs(config_path)
    if args.list:
        return print_host_list(hosts, config_path, args.show_resolved)
    if not hosts:
        raise SSHHomeError("No encontré hosts seleccionables en la config SSH.")
    if args.host and args.host not in hosts:
        raise SSHHomeError(f"El host `{args.host}` no existe en `{config_path}`.")

    with EffectiveSSHConfig(config_path) as effective_config:
        final_session: SSHMasterSession | None = None
        if can_use_tui() and not args.no_tui and args.path is None:
            selection = run_tui(hosts, effective_config, state, initial_host=args.host)
            selected_host = selection.host
            target_path = selection.path
            final_session = selection.session
        else:
            io: PromptIO | None = None
            if args.host:
                selected_host = args.host
            else:
                io = PromptIO()
                selected_host = choose_host_interactively(
                    ordered_hosts(hosts, state, HOST_VIEW_ALL),
                    io,
                )
            if args.show_resolved:
                resolved = _resolve_host_from_effective_config(selected_host, effective_config)
                print("")
                print(f"Alias: {resolved.alias}")
                print(f"HostName: {resolved.hostname or '-'}")
                print(f"User: {resolved.user or '-'}")
                print(f"Port: {resolved.port or '-'}")
                print(f"ProxyJump: {resolved.proxyjump or '-'}")

            if args.path:
                target_path = args.path
            else:
                if io is None:
                    io = PromptIO()
                final_session = SSHMasterSession(selected_host, effective_config)
                try:
                    target_path = browse_remote_path(final_session, io)
                except BaseException:
                    final_session.close()
                    raise

        session = final_session or SSHMasterSession(selected_host, effective_config)
        try:
            exit_code = session.run_interactive(shell_command_for_path(target_path))
            state.record_connection(selected_host, target_path)
            state.save()
            return exit_code
        finally:
            session.close()


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr)
        return 130
    except SSHHomeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
