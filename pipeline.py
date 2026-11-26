"""V-Exporter core: settings, discovery, uncook and export orchestration.

Standard library only.  This module is imported by ``v_exporter.py`` (GUI /
``--selftest``) and never imports tkinter, so every function here is safe to
call from a worker thread.

Pipeline (see research-notes.md):
    1. find NPV folders        <game>\\bin\\x64\\plugins\\cyber_engine_tweaks\\
                               mods\\AppearanceCreatorMod\\NPV\\<Name>\\*.archive
                               (an unpacked ACM draft is the same folder with a
                               ".acm\\draft\\<look>.pkg" set and no archive)
    2. uncook with WolvenKit.CLI into work/<Name>/source/raw  (one tree, -s;
       the add-on's split_source_tree_root() needs the literal "source" segment)
    3. uncook missing base-game dependencies of the entity into the same tree
    4. read <something>.ent.json to list look names
    5. per look: run blender --background --python blender_export.py
    6. write <output>\\<NPV>\\<look>\\<look>.glb/.fbx/.obj,
       <look>_textures\\, proof_<look>.png and export-summary.json
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from material_remap import remap_material_jsons

APP_TITLE = "V-Exporter"

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
LOG_PATH = APP_DIR / "v_exporter.log"
WORK_ROOT = APP_DIR / "work"
BLENDER_SCRIPT = APP_DIR / "blender_export.py"

#: Default output folder, created next to the tool so no machine-specific path
#: is baked into the source.
DEFAULT_OUTPUT_DIR = APP_DIR / "exports"

UNCOOK_TIMEOUT = 3600          # seconds; first uncook of a big NPV is slow
BLENDER_TIMEOUT = 1800         # seconds per look
FORMATS = ("glb", "fbx", "obj")

_LOG_MAX_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, fallback: str = "look", max_length: int = 120) -> str:
    """Turn a user-typed look / NPV name into a valid Windows file name.

    blender_export.py contains the same algorithm for look names; keep the two
    in sync (the GUI uses this one for the NPV sub-folder).
    """
    text = _INVALID_FILENAME.sub("_", str(name))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    if text.upper() in _RESERVED_NAMES:
        text = "_" + text
    if len(text) > max_length:
        text = text[:max_length].rstrip(". ")
    return text or fallback


def unique_filename(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate.lower() in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate.lower())
    return candidate


def default_output_root() -> Path:
    """Default output folder: ``<tool>\\exports``."""
    return DEFAULT_OUTPUT_DIR


def _tail(text: str, lines: int = 40) -> str:
    if not text:
        return ""
    parts = text.rstrip().splitlines()
    return "\n".join(parts[-lines:])


def _normalise_key(path: Path) -> str:
    return str(path).rstrip("\\/").lower()


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = _normalise_key(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

class Logger:
    """Writes timestamped lines to v_exporter.log and forwards to a callback.

    The callback receives ``(level, message)`` and is invoked from whatever
    thread logged the message; the GUI marshals it onto the Tk thread.
    """

    def __init__(self, path: Path = LOG_PATH, echo: Optional[Callable[[str, str], None]] = None):
        self.path = Path(path)
        self.echo = echo
        self._lock = threading.Lock()
        self._rotate()
        try:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None

    def _rotate(self) -> None:
        try:
            if self.path.is_file() and self.path.stat().st_size > _LOG_MAX_BYTES:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
        except OSError:
            pass

    def _write(self, level: str, text: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = str(text).splitlines() or [""]
        with self._lock:
            if self._fh is not None:
                for line in lines:
                    self._fh.write(f"[{timestamp}] {level:<5} {line}\n")
                self._fh.flush()
        if self.echo is not None:
            try:
                self.echo(level, str(text))
            except Exception:
                pass

    def info(self, message: object) -> None:
        self._write("INFO", message)

    def step(self, message: object) -> None:
        self._write("STEP", message)

    def warn(self, message: object) -> None:
        self._write("WARN", message)

    def error(self, message: object) -> None:
        self._write("ERROR", message)

    def raw(self, line: str) -> None:
        """One line of external-tool output."""
        self._write("RAW", f"  | {line}")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


class _NullLogger:
    def info(self, _message: object) -> None: ...
    def step(self, _message: object) -> None: ...
    def warn(self, _message: object) -> None: ...
    def error(self, _message: object) -> None: ...
    def raw(self, _line: str) -> None: ...
    def close(self) -> None: ...


_NULL = _NullLogger()


# ---------------------------------------------------------------------------
# subprocess runner
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    code: int
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


def run_command(
    cmd: Sequence[object],
    logger: object = None,
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
    env: Optional[dict] = None,
) -> CommandResult:
    """Run a command, stream both stdout+stderr into the log, return the code."""
    log = logger or _NULL
    argv = [str(part) for part in cmd]
    log.step("$ " + subprocess.list2cmdline(argv))

    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(cwd) if cwd else None,
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        log.error(f"Could not start {argv[0]}: {exc}")
        return CommandResult(code=-1)

    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_queue.put(line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            line_queue.put(None)

    threading.Thread(target=_reader, name="vx-reader", daemon=True).start()

    result = CommandResult(code=-1)
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        try:
            item = line_queue.get(timeout=1.0)
        except queue.Empty:
            if deadline is not None and time.monotonic() > deadline:
                log.error(f"Timeout after {int(timeout)}s; terminating process.")
                result.timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
            continue
        if item is None:
            break
        result.lines.append(item)
        log.raw(item)

    try:
        result.code = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        result.code = proc.wait()

    if result.code != 0:
        log.warn(f"Process exited with code {result.code}.")
    return result


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    wolvenkit_cli: str = ""
    blender: str = ""
    game_root: str = ""
    npv_root: str = ""
    output_root: str = ""
    render_proofs: bool = True
    entity_overrides: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def copy(self) -> "Settings":
        return Settings(
            wolvenkit_cli=self.wolvenkit_cli,
            blender=self.blender,
            game_root=self.game_root,
            npv_root=self.npv_root,
            output_root=self.output_root,
            render_proofs=self.render_proofs,
            entity_overrides=dict(self.entity_overrides),
            extra=dict(self.extra),
        )


_KNOWN_SETTING_KEYS = {
    "wolvenkit_cli", "blender", "game_root", "npv_root", "output_root",
    "render_proofs", "entity_overrides",
}
_last_settings_error: Optional[str] = None


def blank_settings() -> Settings:
    return Settings(output_root=str(default_output_root()))


def load_settings(path: Path = SETTINGS_PATH, logger: object = None) -> Settings:
    global _last_settings_error
    log = logger or _NULL
    settings = blank_settings()
    if not Path(path).is_file():
        return settings
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        _last_settings_error = f"{path}: {exc}"
        # Preserve the unreadable file instead of silently overwriting it.
        try:
            backup = Path(path).with_suffix(".json.bad")
            shutil.copy2(path, backup)
            log.warn(f"settings.json could not be parsed ({exc}); copied to {backup.name}")
        except OSError:
            log.warn(f"settings.json could not be parsed: {exc}")
        return settings
    if not isinstance(raw, dict):
        return settings
    for key in ("wolvenkit_cli", "blender", "game_root", "npv_root", "output_root"):
        value = raw.get(key)
        if isinstance(value, str):
            setattr(settings, key, value)
    if isinstance(raw.get("render_proofs"), bool):
        settings.render_proofs = bool(raw["render_proofs"])
    overrides = raw.get("entity_overrides")
    if isinstance(overrides, dict):
        settings.entity_overrides = {
            str(k): str(v) for k, v in overrides.items() if isinstance(v, str) and v.strip()
        }
    settings.extra = {k: v for k, v in raw.items() if k not in _KNOWN_SETTING_KEYS}
    if not settings.output_root:
        settings.output_root = str(default_output_root())
    return settings


def save_settings(settings: Settings, path: Path = SETTINGS_PATH, logger: object = None) -> bool:
    payload = {
        "wolvenkit_cli": settings.wolvenkit_cli,
        "blender": settings.blender,
        "game_root": settings.game_root,
        "npv_root": settings.npv_root,
        "output_root": settings.output_root,
        "render_proofs": bool(settings.render_proofs),
        "entity_overrides": settings.entity_overrides,
    }
    payload.update(settings.extra or {})
    try:
        Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        (logger or _NULL).error(f"Could not write {path}: {exc}")
        return False


# ---------------------------------------------------------------------------
# tool / game discovery
# ---------------------------------------------------------------------------

def is_game_root(path: Path) -> bool:
    try:
        return (Path(path) / "bin" / "x64" / "Cyberpunk2077.exe").is_file()
    except OSError:
        return False


def looks_like_game_root(path: Path) -> bool:
    """True for the game install folder (or its ``bin`` folder).

    Accepts a slightly damaged install where only ``bin\\x64`` exists, which is
    enough to derive the ACM NPV location.
    """
    base = Path(path)
    try:
        if (base / "bin" / "x64").is_dir():
            return True
        if (base / "Cyberpunk2077.exe").is_file():
            return True
        if base.name.lower() == "bin" and (base / "x64").is_dir():
            return True
    except OSError:
        return False
    return False


def normalize_game_root(value: str) -> Optional[Path]:
    """Accept a game root, any folder inside it, or the exe itself."""
    if not value:
        return None
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    if path.is_file():
        path = path.parent
    candidates: list[Path] = [path, *list(path.parents)[:4]]
    for name in ("Cyberpunk 2077", "Cyberpunk2077"):
        candidates.append(path / name)
    for candidate in candidates:
        if is_game_root(candidate):
            return candidate
    return None


def npv_root_for_game(game_root: Path) -> Path:
    return (
        Path(game_root)
        / "bin" / "x64" / "plugins" / "cyber_engine_tweaks"
        / "mods" / "AppearanceCreatorMod" / "NPV"
    )


def _mods_dir_for_game(game_root: Path) -> Path:
    return Path(game_root) / "bin" / "x64" / "plugins" / "cyber_engine_tweaks" / "mods"


def _find_child_ci(parent: Path, name: str) -> Optional[Path]:
    try:
        direct = parent / name
        if direct.exists():
            return direct
        lowered = name.lower()
        for entry in parent.iterdir():
            if entry.name.lower() == lowered:
                return entry
    except OSError:
        pass
    return None


def derive_npv_root(game_root: Path) -> Optional[Path]:
    """NPV root for a game install, tolerating ACM folder name variants."""
    acm = acm_mod_dir(game_root)
    if acm is None:
        return None
    return _find_child_ci(acm, "NPV")


def acm_mod_dir(game_root: Path) -> Optional[Path]:
    """The installed AppearanceCreatorMod folder, if any (case-insensitive)."""
    mods = _mods_dir_for_game(Path(game_root))
    acm = _find_child_ci(mods, "AppearanceCreatorMod")
    if acm is not None:
        return acm
    try:
        for entry in mods.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("appearancecreatormod"):
                return entry
    except OSError:
        pass
    return None


def effective_npv_root(settings: Settings, logger: object = None) -> Optional[Path]:
    log = logger or _NULL
    override = (settings.npv_root or "").strip()
    if override:
        resolved = resolve_npv_folder(override, log)
        if resolved is not None:
            return resolved
    game = normalize_game_root(settings.game_root)
    if game is not None:
        derived = derive_npv_root(game)
        if derived is not None and derived.is_dir():
            return derived
    return None


_CORRECTION_WARNED: set[str] = set()


def _log_correction(log: object, given: Path, corrected: Path) -> None:
    key = f"{_normalise_key(Path(given))} -> {_normalise_key(Path(corrected))}"
    if key in _CORRECTION_WARNED:
        return
    _CORRECTION_WARNED.add(key)
    log.warn(f"That's the game folder - using {corrected} instead.")


def resolve_npv_folder(value: str, logger: object = None) -> Optional[Path]:
    """Accept an NPV folder, a game folder, or a path inside the game install.

    A game root (or ``bin`` / ``bin\\x64`` / ``...\\AppearanceCreatorMod``) is
    auto-corrected to ``<game>\\bin\\x64\\plugins\\cyber_engine_tweaks\\mods\\
    AppearanceCreatorMod\\NPV``.  Anything that is not part of the game install
    is returned unchanged so ACM deployed elsewhere still works.
    """
    log = logger or _NULL
    if not value:
        return None
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None

    if looks_like_game_root(path):
        game = normalize_game_root(str(path)) or path
        corrected = derive_npv_root(game) or npv_root_for_game(game)
        try:
            exists = corrected.is_dir()
        except OSError:
            exists = False
        if exists:
            _log_correction(log, path, corrected)
            return corrected
        log.warn(
            f"{path} is the game folder, but the ACM NPV folder does not exist yet "
            f"({corrected}). Create an NPV in game first, then rescan."
        )
        return None

    game = normalize_game_root(str(path))
    if game is not None:
        derived = derive_npv_root(game)
        try:
            exists = derived is not None and derived.is_dir()
        except OSError:
            exists = False
        if exists:
            if _normalise_key(path) != _normalise_key(derived):
                _log_correction(log, path, derived)
            return derived
    return path


def _which(names: Sequence[str]) -> list[Path]:
    found: list[Path] = []
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            found.append(Path(resolved))
    return found


def _files_named(roots: Iterable[Path], filename: str, max_seconds: float = 4.0, max_depth: int = 3) -> list[Path]:
    """Bounded, time-limited recursive search for a file name."""
    deadline = time.monotonic() + max_seconds
    lowered = filename.lower()
    matches: list[Path] = []
    for root in roots:
        if time.monotonic() > deadline:
            break
        try:
            if not Path(root).is_dir():
                continue
        except OSError:
            continue
        stack: list[tuple[Path, int]] = [(Path(root), 0)]
        while stack:
            if time.monotonic() > deadline:
                return matches
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        if entry.name.lower() == lowered:
                            try:
                                if entry.is_file():
                                    matches.append(Path(entry.path))
                            except OSError:
                                pass
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False) and depth + 1 < max_depth:
                                stack.append((Path(entry.path), depth + 1))
                        except OSError:
                            continue
            except OSError:
                continue
    return matches


def find_wolvenkit(logger: object = None) -> Optional[Path]:
    log = logger or _NULL
    candidates = [
        APP_DIR / "WolvenKit.CLI.exe",
        APP_DIR / "tools" / "WolvenKit.CLI.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WolvenKit" / "WolvenKit.CLI.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WolvenKit" / "WolvenKit.CLI.exe",
    ]
    candidates.extend(_which(("WolvenKit.CLI.exe", "wolvenkit.cli")))
    for candidate in candidates:
        try:
            if candidate.is_file():
                log.info(f"WolvenKit CLI detected: {candidate}")
                return candidate
        except OSError:
            continue

    scan_roots = [
        APP_DIR / "tools",
        APP_DIR / "WolvenKit",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WolvenKit",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WolvenKit",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
    ]
    hits = _files_named(scan_roots, "WolvenKit.CLI.exe", max_seconds=5.0, max_depth=4)
    if hits:
        hits.sort(key=lambda p: (len(p.parts), str(p)))
        log.info(f"WolvenKit CLI detected: {hits[0]}")
        return hits[0]
    return None


def _blender_version_key(path: Path) -> tuple:
    match = re.search(r"(\d+)\.(\d+)", str(path))
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return (0, 0)


def find_blender(logger: object = None) -> Optional[Path]:
    log = logger or _NULL
    candidates: list[Path] = []
    candidates.extend(_which(("blender", "blender.exe")))

    bases = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path.home() / "scoop" / "apps" / "blender",
    ]
    for base in bases:
        for sub in ("Blender Foundation", "Blender Foundation/Blender", ""):
            directory = base / sub if sub else base
            try:
                if not directory.is_dir():
                    continue
                for entry in directory.iterdir():
                    exe = entry / "blender.exe"
                    if exe.is_file():
                        candidates.append(exe)
            except OSError:
                continue

    steam_libs = _steam_library_roots()
    for lib in steam_libs:
        for rel in ("steamapps/common/Blender/blender.exe", "common/Blender/blender.exe"):
            candidate = lib / rel
            try:
                if candidate.is_file():
                    candidates.append(candidate)
            except OSError:
                pass

    if not candidates:
        hits = _files_named(
            [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Blender Foundation"],
            "blender.exe", max_seconds=4.0, max_depth=3,
        )
        candidates.extend(hits)

    candidates = _dedupe_paths(candidates)
    candidates.sort(key=_blender_version_key, reverse=True)
    for candidate in candidates:
        try:
            if candidate.is_file():
                log.info(f"Blender detected: {candidate}")
                return candidate
        except OSError:
            continue
    return None


def _fixed_drives() -> list[Path]:
    if os.name != "nt":
        return []
    drives: list[Path] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        path = Path(f"{letter}:\\")
        try:
            if path.exists():
                drives.append(path)
        except OSError:
            continue
    return drives


def _registry_game_paths(logger: object = None) -> list[Path]:
    if os.name != "nt":
        return []
    results: list[Path] = []
    try:
        import winreg  # noqa: PLC0415  (Windows only)
    except ImportError:
        return results
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        key = None
    if key is not None:
        try:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, sub) as subkey:
                        values = {}
                        for value_name in ("gameName", "GameName", "path"):
                            try:
                                values[value_name], _ = winreg.QueryValueEx(subkey, value_name)
                            except OSError:
                                continue
                        label = str(values.get("gameName") or values.get("GameName") or "")
                        location = str(values.get("path") or "")
                        if "cyberpunk" in label.lower() and location:
                            results.append(Path(location))
                except OSError:
                    continue
        finally:
            key.Close()
    return results


def _steam_library_roots(logger: object = None) -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg  # noqa: PLC0415

            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                    steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
                    if steam_path:
                        roots.append(Path(steam_path))
            except OSError:
                pass
        except ImportError:
            pass
    for drive in _fixed_drives():
        for rel in ("Steam", "SteamLibrary", "Games/Steam"):
            candidate = drive / rel
            try:
                if candidate.is_dir():
                    roots.append(candidate)
            except OSError:
                continue
    roots = _dedupe_paths(roots)

    libraries: list[Path] = list(roots)
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            libraries.append(Path(match.group(1).replace("\\\\", "\\")))
    return _dedupe_paths(libraries)


def _steam_game_paths() -> list[Path]:
    results: list[Path] = []
    for library in _steam_library_roots():
        for name in ("Cyberpunk 2077", "Cyberpunk2077"):
            results.append(library / "steamapps" / "common" / name)
    return results


def _epic_game_paths() -> list[Path]:
    results: list[Path] = []
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    manifest_dir = program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    try:
        items = list(manifest_dir.glob("*.item"))
    except OSError:
        items = []
    for item in items:
        try:
            data = json.loads(item.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        label = str(data.get("DisplayName") or "")
        location = str(data.get("InstallLocation") or "")
        if "cyberpunk" in label.lower() and location:
            results.append(Path(location))
    return results


def known_game_paths() -> list[Path]:
    results: list[Path] = []
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    results.extend([
        program_files_x86 / "Steam" / "steamapps" / "common" / "Cyberpunk 2077",
        program_files / "Steam" / "steamapps" / "common" / "Cyberpunk 2077",
        program_files_x86 / "GOG Galaxy" / "Games" / "Cyberpunk 2077",
        program_files / "GOG.com" / "Cyberpunk 2077",
        program_files / "Epic Games" / "Cyberpunk2077",
        program_files_x86 / "Epic Games" / "Cyberpunk2077",
    ])
    results.extend(_registry_game_paths())
    results.extend(_steam_game_paths())
    results.extend(_epic_game_paths())
    for drive in _fixed_drives():
        for rel in (
            "Cyberpunk 2077", "Cyberpunk2077",
            "Games/Cyberpunk 2077", "Games/Cyberpunk2077",
            "Epic Games/Cyberpunk2077", "Epic Games/Cyberpunk 2077",
            "GOG Games/Cyberpunk 2077", "SteamLibrary/steamapps/common/Cyberpunk 2077",
        ):
            results.append(drive / rel)
    return results


def scan_drives_for_game(max_seconds: float = 8.0, max_depth: int = 3) -> list[Path]:
    """Last-resort bounded scan for bin\\x64\\Cyberpunk2077.exe."""
    deadline = time.monotonic() + max_seconds
    found: list[Path] = []
    skip = {
        "windows", "$recycle.bin", "system volume information", "recovery",
        "perflogs", "msocache", "node_modules", "$windows.~bt", "appdata",
        "program files", "program files (x86)", "programdata",
    }
    for drive in _fixed_drives():
        stack: list[tuple[Path, int]] = [(drive, 0)]
        while stack:
            if time.monotonic() > deadline:
                return found
            directory, depth = stack.pop()
            if is_game_root(directory):
                found.append(directory)
                continue
            if depth >= max_depth:
                continue
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.lower() in skip:
                            continue
                        stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return found


def detect_game_root(logger: object = None) -> Optional[Path]:
    log = logger or _NULL
    for candidate in known_game_paths():
        if is_game_root(candidate):
            log.info(f"Game detected: {candidate}")
            return candidate
    found = scan_drives_for_game()
    if found:
        sorted_hits = sorted(found, key=lambda p: (len(p.parts), str(p)))
        log.info(f"Game detected (drive scan): {sorted_hits[0]}")
        return sorted_hits[0]
    return None


# ---------------------------------------------------------------------------
# NPV inventory
# ---------------------------------------------------------------------------

@dataclass
class NpvInfo:
    name: str
    directory: Path
    archives: list[Path] = field(default_factory=list)
    xl_files: list[Path] = field(default_factory=list)
    icon: Optional[Path] = None
    share_zips: list[Path] = field(default_factory=list)
    #: "packed" (has a .archive) or "draft" (only an .acm\ draft, not packed yet).
    status: str = "packed"
    draft_looks: list[str] = field(default_factory=list)

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"

    @property
    def exportable(self) -> bool:
        return not self.is_draft and bool(self.archives)

    @property
    def primary_archive(self) -> Optional[Path]:
        if not self.archives:
            return None
        for archive in self.archives:
            if archive.stem.lower() == self.name.lower():
                return archive
        return sorted(self.archives, key=lambda p: (len(p.name), p.name))[0]

    @property
    def extra_archives(self) -> list[Path]:
        primary = self.primary_archive
        return [a for a in self.archives if a != primary]


def draft_message(npv: "NpvInfo") -> str:
    looks = ", ".join(npv.draft_looks) if npv.draft_looks else "none parsed"
    return (
        f"Draft - looks saved ({looks}) but not packed. "
        "In-game: ACM NPV tab -> press Create NPV, then rescan."
    )


def draft_look_names(acm_dir: Path) -> list[str]:
    """Look names of an unpacked ACM draft: stems of ``.acm\\draft\\*.pkg``."""
    draft = Path(acm_dir) / "draft"
    try:
        entries = sorted(draft.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix.lower() != ".pkg":
                continue
        except OSError:
            continue
        stem = entry.stem
        if not stem or stem.lower() in seen:
            continue
        seen.add(stem.lower())
        names.append(stem)
    return names


def list_npvs(npv_root: Path, logger: object = None) -> list[NpvInfo]:
    """Packed and draft NPV folders under ``npv_root``.

    A sub-folder only counts as an NPV when it has a ``.archive`` or an ``.acm``
    draft folder; everything else is skipped with an INFO line (e.g. when the
    user accidentally pointed the tool at the game root).
    """
    log = logger or _NULL
    items: list[NpvInfo] = []
    try:
        entries = sorted(
            (entry for entry in Path(npv_root).iterdir() if entry.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return items
    for directory in entries:
        archives = sorted(directory.glob("*.archive"))
        xl_files = sorted(directory.glob("*.archive.xl"))
        acm_dir = directory / ".acm"
        try:
            is_draft = not archives and acm_dir.is_dir()
        except OSError:
            is_draft = False
        if not archives and not is_draft:
            log.info(f"{directory.name}: skipped - no .archive and no .acm draft folder (not an NPV).")
            continue
        icons = [p for p in (directory / "icon.png", directory / "icon.jpg") if p.is_file()]
        zips = sorted(directory.glob("*.zip"))
        items.append(NpvInfo(
            name=directory.name,
            directory=directory,
            archives=archives,
            xl_files=xl_files,
            icon=icons[0] if icons else None,
            share_zips=zips,
            status="draft" if is_draft else "packed",
            draft_looks=draft_look_names(acm_dir) if is_draft else [],
        ))
    return items


# ---------------------------------------------------------------------------
# look discovery
# ---------------------------------------------------------------------------

@dataclass
class EntCandidate:
    path: Path
    looks: list[str]
    score: int
    reason: str


@dataclass
class LookScan:
    ent_json: Optional[Path] = None
    looks: list[str] = field(default_factory=list)
    candidates: list[EntCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_dir: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.ent_json is not None


_APPEARANCE_KEY = re.compile(r"appearances?(?:names|list)?$", re.IGNORECASE)


def _cname_value(value: object) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("$value", "value", "Value", "name", "Name"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _appearance_entry_name(item: object) -> Optional[str]:
    """Name for one entry of an ``appearances`` array, whatever its shape.

    Handles plain CNAMEs, ``{"name": ...}`` dicts and WolvenKit's handle
    serialization where a ``.app.json`` entry wraps the definition in ``Data``.
    """
    if isinstance(item, dict):
        for name_key in ("name", "Name", "appearanceName", "AppearanceName"):
            name = _cname_value(item.get(name_key))
            if name:
                return name
        data = item.get("Data")
        if isinstance(data, dict):
            for name_key in ("name", "Name", "appearanceName", "AppearanceName"):
                name = _cname_value(data.get(name_key))
                if name:
                    return name
        # CNAME wrapper entry: {"$type": "CName", "$value": "look_name"}
        return _cname_value(item)
    return _cname_value(item)


def collect_appearance_names(data: object) -> list[str]:
    """Recursively collect names from every ``appearances``-like list.

    Tolerates schema variation: entries may be dicts with ``name`` /
    ``appearanceName``, plain strings, or ``{"$value": "..."}`` CName wrappers.
    """
    found: list[str] = []

    def walk(node: object, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list) and _APPEARANCE_KEY.search(str(key)):
                    for item in value:
                        name = _appearance_entry_name(item)
                        if name:
                            found.append(name)
                elif isinstance(value, dict) and "$value" in value and isinstance(value["$value"], list) \
                        and _APPEARANCE_KEY.search(str(key)):
                    for item in value["$value"]:
                        name = _appearance_entry_name(item)
                        if name:
                            found.append(name)
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data, 0)
    seen: set[str] = set()
    unique: list[str] = []
    for name in found:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _read_json(path: Path) -> Optional[object]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _score_entity(path: Path, looks: list[str], npv_name: str, raw_dir: Path) -> EntCandidate:
    rel = _rel(path, raw_dir).replace("/", "\\").lower()
    score = 0
    reasons: list[str] = []
    if looks:
        score += 100
        reasons.append(f"{len(looks)} appearance(s)")
    lowered_name = npv_name.lower()
    if lowered_name and lowered_name in rel:
        score += 60
        reasons.append("path mentions the NPV name")
    if re.search(r"\\mod\\", rel) or rel.startswith("mod\\"):
        score += 25
        reasons.append("under a mod\\ depot path")
    if 0 < len(looks) <= 40:
        score += 10
        reasons.append("plausible character entity size")
    score -= min(len(path.parts), 20)
    return EntCandidate(path=path, looks=looks, score=score, reason=", ".join(reasons) or "no appearances")


def discover_looks(raw_dir: Path, npv_name: str, logger: object = None, limit: int = 10) -> LookScan:
    log = logger or _NULL
    raw_dir = Path(raw_dir)
    scan = LookScan(raw_dir=raw_dir)
    ent_files = sorted(raw_dir.rglob("*.ent.json")) if raw_dir.is_dir() else []
    if not ent_files:
        scan.notes.append("No *.ent.json found in the uncooked output.")
        log.warn(scan.notes[-1])
        return scan

    candidates: list[EntCandidate] = []
    for ent_file in ent_files:
        data = _read_json(ent_file)
        looks = collect_appearance_names(data) if data is not None else []
        candidates.append(_score_entity(ent_file, looks, npv_name, raw_dir))
    candidates.sort(key=lambda c: (-c.score, str(c.path)))
    scan.candidates = candidates

    log.info(f"Entity candidates ({len(candidates)}):")
    for candidate in candidates[:limit]:
        log.info(f"  {_rel(candidate.path, raw_dir)} - {len(candidate.looks)} look(s) - {candidate.reason}")

    best = candidates[0]
    scan.ent_json = best.path
    scan.looks = list(best.looks)

    if not scan.looks:
        app_looks: list[str] = []
        for app_file in sorted(raw_dir.rglob("*.app.json")):
            data = _read_json(app_file)
            if data is None:
                continue
            app_looks.extend(collect_appearance_names(data))
        seen: set[str] = set()
        recovered: list[str] = []
        for name in app_looks:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                recovered.append(name)
        if recovered:
            scan.notes.append(
                f"Entity JSON contained no appearance list; recovered {len(recovered)} name(s) from .app.json files."
            )
            log.warn(scan.notes[-1])
            scan.looks = recovered
        else:
            scan.notes.append(
                "No appearance names found; the exporter will import ALL appearances and export each collection."
            )
            log.warn(scan.notes[-1])

    if len(candidates) > 1 and any(c.looks for c in candidates[1:limit]):
        scan.notes.append(
            f"{len(candidates)} entities found; using the best match. "
            f"Set settings.json \"entity_overrides\": {{\"{npv_name}\": \"<full .ent.json path>\"}} to force another."
        )
        log.warn(scan.notes[-1])

    if scan.looks:
        log.info(f"Looks for {npv_name}: " + ", ".join(scan.looks))
    return scan


# ---------------------------------------------------------------------------
# uncook (WolvenKit.CLI)
# ---------------------------------------------------------------------------

def work_dir(npv_name: str) -> Path:
    return WORK_ROOT / safe_filename(npv_name, fallback="npv")


def raw_dir_for(npv_name: str) -> Path:
    # The CP77 Blender add-on resolves depot paths with split_source_tree_root(),
    # which literally requires a "source/<tree>" segment in the path; without it
    # it falls back to the parent folder, doubles the path and finds 0 meshes.
    return work_dir(npv_name) / "source" / "raw"


def migrate_legacy_cache(npv_name: str, logger: object = None) -> None:
    """Move an old work/<npv>/raw cache into the source/raw layout (when safe)."""
    log = logger or _NULL
    legacy = work_dir(npv_name) / "raw"
    current = raw_dir_for(npv_name)
    try:
        if not legacy.is_dir():
            return
        if not current.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(current))
            log.info(f"Moved legacy uncook cache to the source/raw layout: {current}")
        else:
            log.info(f"Legacy uncook cache left in place (source/raw already exists): {legacy}")
    except OSError as exc:
        log.warn(f"Could not migrate the legacy uncook cache {legacy}: {exc}")


def _stamp_path(npv_name: str) -> Path:
    return work_dir(npv_name) / "uncook.json"


def _read_stamp(npv_name: str) -> Optional[dict]:
    data = _read_json(_stamp_path(npv_name))
    return data if isinstance(data, dict) else None


def _stamp_matches(stamp: Optional[dict], archive: Path, cli: Path) -> bool:
    if not stamp:
        return False
    try:
        stat = archive.stat()
        cli_stat = cli.stat()
    except OSError:
        return False
    return (
        str(stamp.get("archive")) == str(archive)
        and stamp.get("archive_size") == stat.st_size
        and stamp.get("archive_mtime") == int(stat.st_mtime)
        and stamp.get("cli_mtime") == int(cli_stat.st_mtime)
        and stamp.get("ok") is True
    )


def _write_stamp(npv_name: str, archive: Path, cli: Path) -> None:
    try:
        stat = archive.stat()
        cli_stat = cli.stat()
        payload = {
            "ok": True,
            "archive": str(archive),
            "archive_size": stat.st_size,
            "archive_mtime": int(stat.st_mtime),
            "cli_mtime": int(cli_stat.st_mtime),
            "uncooked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _stamp_path(npv_name).parent.mkdir(parents=True, exist_ok=True)
        _stamp_path(npv_name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


class SetupError(RuntimeError):
    """User-actionable setup / environment problem."""


class PipelineError(RuntimeError):
    """A pipeline step failed (bad archive, add-on mismatch, ...)."""


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_WOLVENKIT_PROBE_CACHE: dict[str, dict] = {}
_WOLVENKIT_OLD_WARNED: set[str] = set()

_WOLVENKIT_OLD_WARNING = (
    "WolvenKit CLI {version} is a pre-2.5 build - game 2.5 needs the v10 beta console asset "
    "(WolvenKit.Console-10.x zip from github.com/WolvenKit/WolvenKit/releases). "
    "Uncook results are not guaranteed."
)


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """First ``major.minor[.patch]`` in a CLI's output, or None."""
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def version_text(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def probe_wolvenkit(cli: Path, logger: object = None, refresh: bool = False) -> dict:
    """Return {'ok', 'code', 'version', 'output'} for WolvenKit.CLI.exe.

    Old 9.x builds cannot read game 2.5 archives; they are flagged with a WARN
    (not a FAIL - some commands still work).  Memoised per path + mtime.
    """
    log = logger or _NULL
    cli = Path(cli)
    key = str(cli)
    try:
        mtime = cli.stat().st_mtime
    except OSError:
        mtime = 0
    cached = _WOLVENKIT_PROBE_CACHE.get(key)
    if cached and cached.get("mtime") == mtime and not refresh:
        info = cached["info"]
    else:
        result = run_command([cli, "--version"], logger=log, timeout=180)
        code = result.code
        output = result.output
        if code != 0 or not output.strip():
            fallback = run_command([cli, "--help"], logger=log, timeout=180)
            output = (output + "\n" + fallback.output).strip()
            if code != 0:
                code = fallback.code
        info = {"ok": code == 0, "code": code, "version": parse_version(output), "output": output}
        _WOLVENKIT_PROBE_CACHE[key] = {"mtime": mtime, "info": info}

    version = info.get("version")
    if version is not None and version[0] < 10:
        text = version_text(version)
        if text not in _WOLVENKIT_OLD_WARNED:
            _WOLVENKIT_OLD_WARNED.add(text)
            log.warn(_WOLVENKIT_OLD_WARNING.format(version=text))
    return info


def cached_looks(npv_name: str) -> Optional[LookScan]:
    """Look discovery from an existing uncook cache, without touching the CLI."""
    migrate_legacy_cache(npv_name)
    raw = raw_dir_for(npv_name)
    try:
        if not raw.is_dir() or not any(raw.iterdir()):
            return None
    except OSError:
        return None
    stamp = _read_stamp(npv_name)
    if stamp and stamp.get("ok") is not True:
        return None
    return discover_looks(raw, npv_name, _NULL)


def uncook_npv(npv: NpvInfo, settings: Settings, logger: object = None, force: bool = False) -> Path:
    log = logger or _NULL
    cli_value = (settings.wolvenkit_cli or "").strip()
    if not cli_value:
        raise SetupError(
            "WolvenKit.CLI.exe is not configured.\n\n"
            "Download WolvenKit.Console-<version>.zip (10.0+ needed for game 2.5) from "
            "https://github.com/WolvenKit/WolvenKit/releases, extract it, then use "
            "'Browse' in the WolvenKit.CLI row. See README.md."
        )
    cli = Path(cli_value)
    if not cli.is_file():
        raise SetupError(
            f"WolvenKit.CLI.exe was not found at:\n{cli}\n\n"
            "Fix the path in the settings row (Browse) or re-download "
            "WolvenKit.Console-<version>.zip from the WolvenKit releases page."
        )
    probe_wolvenkit(cli, log)

    archive = npv.primary_archive
    if archive is None:
        if npv.is_draft:
            raise SetupError(
                f"{npv.name}: {draft_message(npv)}\n\nDraft folder:\n{npv.directory}"
            )
        raise SetupError(
            f"{npv.name}: no .archive file and no .acm draft folder in\n{npv.directory}\n\n"
            "This does not look like an NPV folder. Create the NPV with ACM's 'Create NPV' "
            "button (ACM 2.71+), then rescan."
        )
    if npv.extra_archives:
        log.warn(
            f"{npv.name}: {len(npv.extra_archives)} extra .archive file(s) ignored: "
            + ", ".join(p.name for p in npv.extra_archives)
        )
    if not npv.xl_files:
        log.warn(
            f"{npv.name}: no .archive.xl next to the archive. If the NPV was shared, "
            "make sure the .archive and .archive.xl come from the same 'Create NPV' run."
        )

    raw = raw_dir_for(npv.name)
    migrate_legacy_cache(npv.name, log)
    stamp = _read_stamp(npv.name)
    if not force and raw.is_dir() and _stamp_matches(stamp, archive, cli):
        try:
            if any(raw.iterdir()):
                if _has_glb_meshes(raw):
                    log.info(f"Reusing cached uncook: {raw}")
                    return raw
                # An older cache was uncooked with -s only: no .glb meshes yet.
                log.info("Cached uncook has no .glb meshes; running the mesh buffer pass ...")
                _uncook_mesh_buffers(cli, archive, raw, log)
                return raw
        except OSError:
            pass

    log.step(f"Uncooking {archive.name} (first run can take several minutes) ...")
    try:
        if raw.exists():
            shutil.rmtree(raw, ignore_errors=True)
        raw.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(f"Could not prepare the work folder {raw}: {exc}") from exc

    base = [cli, "uncook", str(archive), "-o", str(raw), "-s", "--uext", "png"]
    # NOTE: never add -gp to an uncook call. WolvenKit 10 ignores the given archive
    # as soon as -gp is present and uncooks every archive in the install (~200 GB).
    variants = [
        base,
        [cli, "uncook", str(archive), "-o", str(raw)],
    ]

    last: Optional[CommandResult] = None
    for index, cmd in enumerate(variants):
        if index:
            log.warn("Retrying the uncook with a reduced option set (older WolvenKit CLI?).")
        result = run_command(cmd, logger=log, timeout=UNCOOK_TIMEOUT)
        last = result
        try:
            produced = raw.is_dir() and any(raw.iterdir())
        except OSError:
            produced = False
        if result.code == 0 and produced:
            _write_stamp(npv.name, archive, cli)
            if index:
                log.warn(
                    "Uncook succeeded without --mesh-export-type/--mesh-export-lod-filter. "
                    "LOD meshes may be included; upgrade to WolvenKit 10.x for game 2.5."
                )
            log.info(f"Uncooked to {raw}")
            return raw
        text = result.output.lower()
        option_problem = any(
            token in text
            for token in ("unrecognized", "unknown option", "not recognized", "option", "argument")
        )
        # Remove partial output so the next attempt starts clean.
        shutil.rmtree(raw, ignore_errors=True)
        try:
            raw.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not option_problem:
            break

    shutil.rmtree(raw, ignore_errors=True)
    detail = _tail(last.output if last else "", 25)
    hint = (
        "Uncook failed.\n\n"
        "Likely causes:\n"
        "• WolvenKit older than 10.0 (game 2.5 needs 10.0.0-beta.1+ and the .NET 10 runtime).\n"
        "• The NPV archive was created with ACM's 'Protected' option; recreate it with Protected off.\n"
        "• The .archive file is corrupted / incomplete.\n\n"
        "Last CLI output:\n" + (detail or "(none)")
    )
    raise PipelineError(hint)


# ---------------------------------------------------------------------------
# dependency uncook (base-game refs referenced by the NPV entity)
# ---------------------------------------------------------------------------

_DEP_REFS = (".rig", ".mesh", ".morphtarget", ".app", ".animgraph", ".ent")


def _depot_to_local(raw_dir: Path, ref: str) -> Path:
    rel = str(ref).replace("\\", "/").lstrip("/")
    return Path(raw_dir).joinpath(*[part for part in rel.split("/") if part])


def _walk_refs(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _walk_refs(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_refs(item, found)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > 4 and text.lower().endswith(_DEP_REFS) and ("\\" in text or "/" in text):
            found.add(text)


def collect_refs(paths: Sequence[Path]) -> list[str]:
    """All depot-path strings (.rig/.mesh/.app/...) referenced by JSON files."""
    found: set[str] = set()
    for path in paths:
        data = _read_json(Path(path))
        if data is not None:
            _walk_refs(data, found)
    return sorted(found)


def _dep_present(raw_dir: Path, ref: str) -> bool:
    local = _depot_to_local(raw_dir, ref)
    candidates = (local, Path(str(local) + ".json"), Path(str(local) + ".glb"))
    return any(candidate.exists() for candidate in candidates)


def _game_archives(game: str) -> list[Path]:
    """Base-game archives to search explicitly (a bare ``-gp`` uncook is unsafe)."""
    root = Path(game) / "archive" / "pc"
    found: list[Path] = []
    for sub in ("content", "ep1", "bonus"):
        folder = root / sub
        try:
            if folder.is_dir():
                found.extend(sorted(p for p in folder.glob("*.archive") if p.is_file()))
        except OSError:
            continue
    return found


def _extract_rig(cli: Path, archives: Sequence[Path], ref: str, raw_dir: Path, log: object) -> bool:
    """Unbundle a .rig from the game archives and serialize it to .rig.json.

    ``uncook`` skips .rig (filtered by CR2W CanUncook); ``extract`` (unbundle)
    still finds it, and ``convert serialize`` writes the json the add-on reads.
    """
    rel = ref.replace("\\", "/").lstrip("/")
    stem = Path(rel).stem
    target = Path(str(_depot_to_local(raw_dir, ref)) + ".json")
    tmp = Path(raw_dir).parent / "_rig_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        cmd = [
            cli, "extract",
            *[str(archive) for archive in archives],
            "-o", str(tmp),
            "-w", f"*{stem}.rig",
        ]
        run_command(cmd, logger=log, timeout=UNCOOK_TIMEOUT)
        rigs = sorted(tmp.rglob("*.rig"))
        if not rigs:
            log.warn(f"  .rig not present in the game archives: {ref}")
            return False
        run_command([cli, "convert", "serialize", *[str(r) for r in rigs]], logger=log,
                    timeout=UNCOOK_TIMEOUT)
        done = next((r for r in rigs if Path(str(r) + ".json").is_file()), None)
        if done is None:
            log.warn(f"  .rig could not be serialized: {ref}")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(done) + ".json", str(target))
        log.info(f"  .rig resolved via extract+serialize: {target.name}")
        return True
    except OSError as exc:
        log.warn(f"  .rig extract failed ({ref}): {exc}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def uncook_dependencies(
    raw_dir: Path,
    ent_json: Optional[Path],
    settings: Settings,
    logger: object = None,
) -> dict:
    """Uncook base-game refs of the entity/app(s) that are missing from raw_dir.

    Never raises: missing base-game data (notably .rig, see README) is logged and
    reported in ``warnings`` so the export can continue unrigged.
    """
    log = logger or _NULL
    info: dict = {"requested": 0, "uncooked": 0, "missing": [], "warnings": [], "rig_unavailable": False}
    if ent_json is None or not Path(ent_json).is_file():
        return info
    raw_dir = Path(raw_dir)
    cli_value = (settings.wolvenkit_cli or "").strip()
    game = (settings.game_root or "").strip()
    if not cli_value or not game:
        info["warnings"].append("Dependency uncook skipped: wolvenkit_cli/game_root not configured.")
        return info
    cli = Path(cli_value)

    # Entity first, then one closure pass over the reachable .app.json files.
    scanned: set[Path] = set()
    queue_paths: list[Path] = [Path(ent_json)]
    refs: set[str] = set()
    for _ in range(4):
        fresh = [p for p in queue_paths if p not in scanned]
        if not fresh:
            break
        scanned.update(fresh)
        refs.update(collect_refs(fresh))
        next_paths: list[Path] = []
        for ref in refs:
            if not ref.lower().endswith(".app"):
                continue
            local = _depot_to_local(raw_dir, ref)
            candidate = local if local.suffix == ".json" else Path(str(local) + ".json")
            if candidate.is_file() and candidate not in scanned:
                next_paths.append(candidate)
        queue_paths = next_paths

    missing = [ref for ref in refs if not _dep_present(raw_dir, ref)]
    info["requested"] = len(refs)
    info["missing"] = missing
    if not missing:
        return info

    log.info(f"Uncooking {len(missing)} missing base-game dependency file(s) into {raw_dir} ...")
    # Never pass -gp to "uncook": v10 then ignores the given archives and uncooks the
    # whole install (~200 GB).  The archive files are listed explicitly instead.
    archives = _game_archives(game)
    if not archives:
        note = (
            "Dependency uncook skipped: no .archive files under "
            f"{Path(game) / 'archive' / 'pc'}"
        )
        log.warn(note)
        info["warnings"].append(note)
        return info

    by_ext: dict[str, list[str]] = {}
    for ref in missing:
        rel = ref.replace("\\", "/").lstrip("/")
        by_ext.setdefault(Path(rel).suffix.lower(), []).append(ref)

    raw_dir.mkdir(parents=True, exist_ok=True)
    rigs = by_ext.pop(".rig", [])
    for ext, group in sorted(by_ext.items()):
        landed = 0
        for ref in group:
            if _dep_present(raw_dir, ref):
                landed += 1
                continue
            rel = ref.replace("\\", "/").lstrip("/")
            cmd = [
                cli, "uncook",
                *[str(archive) for archive in archives],
                "-o", str(raw_dir),
                "-s",
                "--uext", "png",
                "-w", f"*{Path(rel).stem}*",
            ]
            run_command(cmd, logger=log, timeout=UNCOOK_TIMEOUT)
            if _dep_present(raw_dir, ref):
                landed += 1
        info["uncooked"] += landed
        if landed:
            log.info(f"  {ext}: resolved {landed}/{len(group)} dependency file(s)")
            continue
        note = (
            f"{ext}: {len(group)} referenced file(s) could not be uncooked from the vanilla "
            "archives (pattern matched nothing)."
        )
        log.warn(f"  {note}")
        info["warnings"].append(note)

    # .rig is filtered out of "uncook" (CR2W CanUncook), but it can be unbundled.
    rig_missing: list[str] = []
    rig_resolved = 0
    for ref in rigs:
        if _dep_present(raw_dir, ref):
            info["uncooked"] += 1
            rig_resolved += 1
            continue
        if _extract_rig(cli, archives, ref, raw_dir, log):
            info["uncooked"] += 1
            rig_resolved += 1
            continue
        rig_missing.append(ref)
        note = f".rig: {ref} could not be resolved from the vanilla archives."
        log.warn(f"  {note}")
        info["warnings"].append(note)
    if rigs and not rig_resolved:
        info["rig_unavailable"] = True
        log.warn("  Rig unavailable; meshes will import unrigged (static bind pose).")
    elif rig_missing:
        log.warn(
            f"  {len(rig_missing)}/{len(rigs)} rig(s) are NPV-local (not in the game archives); "
            "those components import unrigged, the rest of the entity keeps its rig."
        )
    return info


# ---------------------------------------------------------------------------
# mesh -> glb (WolvenKit export, needs the MaterialRepo depot)
# ---------------------------------------------------------------------------

MESH_EXPORT_TIMEOUT = 900      # seconds per mesh

#: Config section of MeshExportArgs.  Key names are taken from the WolvenKit
#: sources: ``Uncook.Cr2wTools/ExportMesh`` reads ``meshExportArgs.withMaterials``
#: + ``meshExportArgs.MaterialRepo`` ("Depot path is not set..." is raised when
#: MaterialRepo is null) and ``ExportCommand`` binds the section through
#: ``IOptions<MeshExportArgs>`` (appsettings.json, section = class name).
_APPSETTINGS_NAME = "appsettings.json"
_MESH_ARGS_DEFAULTS = {
    "withMaterials": True,
    "isGLBinary": True,
    "LodFilter": True,
    "meshExportType": "MeshOnly",
    "MaterialUncookExtension": "png",
}


def appsettings_path_for(cli: Path) -> Path:
    """appsettings.json that belongs to the WolvenKit.CLI.exe build."""
    return Path(cli).resolve().parent / _APPSETTINGS_NAME


def _meshes(raw_dir: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(raw_dir).rglob("*.mesh") if p.is_file())
    except OSError:
        return []


def _has_glb_meshes(raw_dir: Path) -> bool:
    """True when at least one .glb exists under raw_dir (fast cache probe)."""
    try:
        return any(Path(raw_dir).rglob("*.glb"))
    except OSError:
        return False


def _glb_for(mesh: Path) -> Path:
    return Path(str(mesh)[:-len(".mesh")] + ".glb")


def _uncook_mesh_buffers(cli: Path, archive: Path, raw: Path, logger: object = None) -> bool:
    """Second uncook pass WITHOUT -s: writes mesh .glb + texture pngs.

    ``-s`` (serialize) makes WolvenKit write the .json and return before the
    buffer step, so a serialize-only pass has no glb.  Never pass -gp here:
    with -gp v10 ignores the given archive and uncooks the whole install.
    """
    log = logger or _NULL
    cmd = [
        str(cli), "uncook", str(archive),
        "-o", str(raw),
        "--uext", "png",
        "--mesh-export-lod-filter",
    ]
    result = run_command(cmd, logger=log, timeout=UNCOOK_TIMEOUT)
    if not _has_glb_meshes(raw):
        log.warn(
            "Mesh buffer pass produced no .glb files; meshes will be exported one by one "
            "with the export command instead. Last CLI output:\n" + _tail(result.output, 6)
        )
        return False
    return True


def patch_mesh_export_args(
    cli: Path,
    repo: Path,
    logger: object = None,
) -> dict:
    """Point MeshExportArgs.MaterialRepo at ``repo`` (and keep the GLB flags).

    WolvenKit's ExportMesh only writes a .glb with materials when
    MaterialRepo is set; each NPV needs its own uncook tree as its depot.
    Returns {'ok', 'path', 'error'} - never raises.
    """
    log = logger or _NULL
    path = appsettings_path_for(cli)
    try:
        data: dict = {}
        if path.is_file():
            loaded = _read_json(path)
            if isinstance(loaded, dict):
                data = loaded
        section = data.get("MeshExportArgs")
        if not isinstance(section, dict):
            section = {}
        for key, value in _MESH_ARGS_DEFAULTS.items():
            section.setdefault(key, value)
        section["MaterialRepo"] = str(Path(repo).resolve())
        data["MeshExportArgs"] = section
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        return {"ok": True, "path": str(path), "error": None}
    except OSError as exc:
        log.warn(f"Could not patch {path}: {exc}")
        return {"ok": False, "path": str(path), "error": str(exc)}


#: ``work/<npv>/source/raw/base/acm_npv/<slug>/`` prefix that the NPV archive
#: adds in front of the vanilla depot path (generic: works for any NPV slug).
_NPV_DEPOT_PREFIX = re.compile(r"(?:^|/)base/acm_npv/[^/]+/", re.IGNORECASE)

#: ``- <archive>: Found 1/83419 entries to extract.`` lines of extract/uncook.
_ARCHIVE_FOUND = re.compile(r"-\s+(.+?\.archive):\s+Found\s+(\d+)/", re.IGNORECASE)


def _vanilla_rel_path(mesh: Path, raw_dir: Path) -> Optional[str]:
    """Depot path of ``mesh`` without the ``base/acm_npv/<slug>/`` prefix."""
    try:
        rel = Path(mesh).resolve().relative_to(Path(raw_dir).resolve())
    except (OSError, ValueError):
        return None
    text = str(rel).replace("\\", "/").lstrip("/")
    return _NPV_DEPOT_PREFIX.sub("", text, count=1) or None


def _is_valid_glb(path: Path) -> bool:
    """True for a non-empty file that starts with the glTF binary magic."""
    try:
        if Path(path).stat().st_size <= 12:
            return False
        with open(path, "rb") as handle:
            return handle.read(4) == b"glTF"
    except OSError:
        return False


def _matches_in_output(result: CommandResult) -> list[str]:
    """Archives reported as containing an extract match (``Found N/...``)."""
    archives: list[str] = []
    for line in result.lines:
        match = _ARCHIVE_FOUND.search(line)
        if match and int(match.group(2)) > 0:
            archives.append(match.group(1).strip())
    return archives


def _heal_search_targets(game: str) -> list[list[Path]]:
    """Archive sets searched for a healed copy, canonical base game first.

    A folder is passed as a one-item target: ``extract`` accepts folders and scans
    every archive in one invocation, which keeps the mod search to a few seconds.
    """
    targets: list[list[Path]] = []
    archives = _game_archives(game)
    if archives:
        targets.append(archives)
    mods = Path(game) / "archive" / "pc" / "mod"
    try:
        if mods.is_dir() and any(mods.glob("*.archive")):
            targets.append([mods])
    except OSError:
        pass
    return targets


def _find_extracted(work_root: Path, rel_path: str, stem: str) -> Optional[Path]:
    """Locate the unbundled ``<stem>.mesh`` whose depot path matches ``rel_path``."""
    wanted = rel_path.replace("\\", "/").lower()
    fallback: list[Path] = []
    try:
        candidates = sorted(Path(work_root).rglob(f"{stem}.mesh"))
    except OSError:
        return None
    for path in candidates:
        try:
            rel = str(path.relative_to(work_root)).replace("\\", "/").lower()
        except ValueError:
            continue
        if rel == wanted:
            return path
        fallback.append(path)
    return fallback[0] if len(fallback) == 1 else None


def _export_mesh_to_glb(cli: Path, mesh: Path, game: str, log: object) -> Optional[Path]:
    """Run ``export`` on an extracted .mesh; return its valid .glb or None."""
    try:
        run_command(
            [cli, "export", str(mesh), "-o", str(mesh.parent), "-gp", game],
            logger=log,
            timeout=MESH_EXPORT_TIMEOUT,
        )
    except Exception:                                  # never crash the pipeline
        return None
    glb = _glb_for(mesh)
    return glb if _is_valid_glb(glb) else None


def _heal_from_archives(
    mesh_path: Path,
    raw_dir: Path,
    settings: Settings,
    logger: object = None,
) -> bool:
    """Recover a corrupt cached .mesh from a healthy archive copy.

    An NPV archive occasionally ships a truncated .mesh; ``export`` then dies with
    "Unable to read beyond the end of the stream" and writes no .glb.  The same
    depot path usually still exists in the base game or a mod archive: unbundle
    it, export that copy and drop the .glb (plus Material.json) next to the cached
    mesh, which is where the CP77 add-on looks for geometry.  Never raises.
    """
    log = logger or _NULL
    mesh_path = Path(mesh_path)
    cli_value = (settings.wolvenkit_cli or "").strip()
    game = (settings.game_root or "").strip()
    rel = _vanilla_rel_path(mesh_path, raw_dir)
    if not cli_value or not Path(cli_value).is_file() or not game or not rel:
        log.warn(f"  heal skipped for {mesh_path.name}: no archive search possible")
        log.warn(f"heal failed for {mesh_path.name}")
        return False
    cli = Path(cli_value)
    stem = Path(rel).stem
    tmp = WORK_ROOT / "_heal" / safe_filename(stem, fallback="mesh")
    healed = False
    try:
        for archives in _heal_search_targets(game):
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                tmp.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warn(f"  heal skipped for {mesh_path.name}: {exc}")
                break
            try:
                result = run_command(
                    [cli, "extract", *[str(item) for item in archives], "-o", str(tmp),
                     "-w", f"*{stem}.mesh"],
                    logger=log,
                    timeout=UNCOOK_TIMEOUT,
                )
            except Exception as exc:                   # never crash the pipeline
                log.warn(f"  heal search failed ({mesh_path.name}): {exc}")
                continue
            source = _find_extracted(tmp, rel, stem)
            if source is None:
                continue
            glb = _export_mesh_to_glb(cli, source, game, log)
            if glb is None:
                log.warn(
                    f"  {mesh_path.name}: healthy copy found but no glb was produced "
                    "(known limitation: the glb exporter needs single-buffer meshes)"
                )
                continue
            shutil.copyfile(glb, _glb_for(mesh_path))
            material = source.parent / f"{stem}.Material.json"
            if material.is_file():
                shutil.copyfile(material, mesh_path.parent / f"{stem}.Material.json")
            origin = ", ".join(_matches_in_output(result)) or str(archives[0])
            log.info(f"healed {mesh_path.name} from {origin}")
            healed = True
            break
    except OSError as exc:
        log.warn(f"  heal failed for {mesh_path.name}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not healed:
        log.warn(f"heal failed for {mesh_path.name}")
    return healed


# ---------------------------------------------------------------------------
# material dependency heal (.mi chains and the textures they reference)
# ---------------------------------------------------------------------------

#: Cached JSON files whose string values are depot paths the add-on resolves.
_MATERIAL_JSON_SUFFIXES = (
    ".material.json", ".mesh.json", ".mi.json", ".mlsetup.json",
    ".mlmask.json", ".mltemplate.json", ".xbm.json",
)

#: Depot extensions referenced from those files.
_MATERIAL_REF_EXTS = (".mi", ".mlsetup", ".mlmask", ".mltemplate", ".xbm", ".dds")

#: ``.mi``-family files: ``uncook`` skips them, ``extract`` finds them and
#: ``convert serialize`` writes the ``<name>.mi.json`` the add-on reads.
_MATERIAL_SERIALIZE_EXTS = (".mi", ".mlsetup", ".mlmask", ".mltemplate")

#: Textures: ``uncook -s`` writes the json without png, a plain pass the png.
_MATERIAL_TEXTURE_EXTS = (".xbm", ".dds")

HEAL_PASSES = 3                 # Material.json -> .mi -> .mi.json -> textures
HEAL_TIME_BUDGET = 30 * 60      # seconds; the heal stops after this

#: Cache-local absolute paths that leak in through ``Header.ArchiveFileName``
#: of the ``.mi.json`` files this healer itself writes.
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:/|//)")


def _material_refs(raw_dir: Path) -> list[str]:
    """Depot refs (``.mi``/``.xbm``...) found in the cached material jsons."""
    found: set[str] = set()
    try:
        paths = sorted(
            p for p in Path(raw_dir).rglob("*.json")
            if p.is_file() and p.name.lower().endswith(_MATERIAL_JSON_SUFFIXES)
        )
    except OSError:
        return []
    for path in paths:
        data = _read_json(path)
        if data is not None:
            _walk_material_refs(data, found)
    return sorted(found)


def _walk_material_refs(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _walk_material_refs(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_material_refs(item, found)
    elif isinstance(value, str):
        text = re.sub(r"/{2,}", "/", value.strip().replace("\\", "/")).lstrip("/*")
        if re.match(r"^[A-Za-z]:/", text):
            return                     # absolute path metadata, e.g. serialize "ArchiveFileName"
        if "/" not in text or not text.lower().endswith(_MATERIAL_REF_EXTS):
            return
        if any(ch in text for ch in "{}*?"):
            return                     # unresolved template, e.g. ``{material}.xbm``
        if _ABSOLUTE_PATH.match(text) or text.lower().startswith("work/"):
            return                     # Header.ArchiveFileName of a healed json
        found.add(text)


def _material_present(raw_dir: Path, ref: str) -> bool:
    """True when the add-on (and the mesh exporter) can resolve ``ref`` here."""
    local = _depot_to_local(raw_dir, ref)
    if local.is_file():
        return True
    if Path(str(local) + ".json").is_file():
        return True
    if ref.lower().endswith(_MATERIAL_TEXTURE_EXTS):
        return local.with_suffix(".png").is_file()
    return False


def _npv_depot_prefix(raw_dir: Path) -> str:
    """``base/acm_npv/<slug>/`` of this cache, or "" for a vanilla tree."""
    root = Path(raw_dir) / "base" / "acm_npv"
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir():
                return f"base/acm_npv/{child.name}/"
    except OSError:
        pass
    return ""


def _find_material_source(
    root: Path,
    wanted: Sequence[str],
    exts: Sequence[str] = _MATERIAL_REF_EXTS,
) -> Optional[Path]:
    """Extracted file whose depot path matches one of ``wanted`` (any NPV prefix)."""
    keys = {str(item).replace("\\", "/").lower() for item in wanted}
    fallback: list[Path] = []
    for ext in exts:
        try:
            candidates = sorted(Path(root).rglob(f"*{ext}"))
        except OSError:
            continue
        for path in candidates:
            try:
                rel = str(path.relative_to(root)).replace("\\", "/").lower()
            except ValueError:
                continue
            if rel in keys or _NPV_DEPOT_PREFIX.sub("", rel, count=1) in keys:
                return path
            fallback.append(path)
    return fallback[0] if len(fallback) == 1 else None


def _safe_depot_rel(rel: object) -> str:
    """A relative depot path, or "" for absolute/escaped junk.

    A healed json can carry an absolute ``ArchiveFileName`` (for example
    ``D:\\Games\\...``); joining the NPV prefix onto that used to build a stray
    ``...\\<slug>\\Games\\...`` tree inside the cache.
    """
    text = str(rel or "").strip()
    if not text:
        return ""
    if Path(text).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text):
        return ""
    posix = text.replace("\\", "/").lstrip("/")
    parts = [part for part in posix.split("/") if part]
    if not parts or ".." in parts or "://" in posix:
        return ""
    # Remains of an absolute path whose root was already peeled off.
    if any(part.endswith(":") for part in parts):
        return ""
    return "/".join(parts)


def _copy_material_file(source: Path, raw_dir: Path, targets: Sequence[str]) -> int:
    """Copy ``source`` to every depot path in ``targets``; returns the count."""
    written = 0
    for rel in targets:
        safe = _safe_depot_rel(rel)
        if not safe:
            continue
        target = _depot_to_local(raw_dir, safe)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            written += 1
        except OSError:
            continue
    return written


def _mirror_material(raw_dir: Path, vanilla_rel: str, prefix: str) -> int:
    """Copy cached siblings between the vanilla and the NPV depot path.

    Whichever side already has the file is the source, so a cache that only
    holds the prefixed copies (an NPV archive ships mod resources that way) is
    not searched in the archives again.  Returns the number of files written,
    or 0 when the artifact the ref needs is still absent (caller searches).
    """
    if not prefix:
        return 0
    vanilla = _depot_to_local(raw_dir, vanilla_rel)
    prefixed = _depot_to_local(raw_dir, prefix + vanilla_rel)
    lower = vanilla_rel.lower()
    textures = lower.endswith(_MATERIAL_TEXTURE_EXTS)
    artifacts: list[tuple[str, Path, Path]] = [
        (vanilla_rel, vanilla, prefixed),
        (vanilla_rel + ".json", Path(str(vanilla) + ".json"), Path(str(prefixed) + ".json")),
    ]
    if textures:
        png_rel = str(Path(vanilla_rel).with_suffix(".png")).replace("\\", "/")
        artifacts.append((
            png_rel, vanilla.with_suffix(".png"), prefixed.with_suffix(".png")))
    written = 0
    for rel, source_vanilla, source_prefixed in artifacts:
        if source_vanilla.is_file() and not source_prefixed.is_file():
            written += _copy_material_file(source_vanilla, raw_dir, [prefix + rel])
        elif source_prefixed.is_file() and not source_vanilla.is_file():
            written += _copy_material_file(source_prefixed, raw_dir, [rel])
    if textures:
        complete = vanilla.is_file() or vanilla.with_suffix(".png").is_file()
    else:
        complete = Path(str(vanilla) + ".json").is_file()
    return written if complete else 0


def _heal_material_ref(
    cli: Path,
    archives: Sequence[Path],
    vanilla_rel: str,
    raw_dir: Path,
    prefix: str,
    logger: object = None,
) -> int:
    """Unbundle one missing material dependency and mirror it into the depot.

    Returns the number of depot files written (0 = not in these archives).
    """
    log = logger or _NULL
    vanilla_rel = _safe_depot_rel(vanilla_rel)
    if not vanilla_rel:
        log.warn("  material heal skipped: reference is not a depot-relative path")
        return 0
    ext = Path(vanilla_rel.lower()).suffix
    stem = Path(vanilla_rel.lower()).stem
    tmp = WORK_ROOT / "_miheal" / safe_filename(stem, fallback="mat")
    shutil.rmtree(tmp, ignore_errors=True)
    targets = [vanilla_rel] + ([prefix + vanilla_rel] if prefix else [])
    archive_args = [str(item) for item in archives]
    written = 0
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        if ext in _MATERIAL_SERIALIZE_EXTS:
            run_command(
                [cli, "extract", *archive_args, "-o", str(tmp), "-w", f"*{stem}*"],
                logger=log, timeout=UNCOOK_TIMEOUT,
            )
            source = _find_material_source(tmp, [vanilla_rel], _MATERIAL_SERIALIZE_EXTS)
            if source is None:
                return 0
            try:
                run_command([cli, "convert", "serialize", str(source)],
                            logger=log, timeout=UNCOOK_TIMEOUT)
            except Exception as exc:                   # never crash the pipeline
                log.warn(f"  material serialize failed ({vanilla_rel}): {exc}")
            written += _copy_material_file(source, raw_dir, targets)
            json_source = Path(str(source) + ".json")
            if json_source.is_file():
                written += _copy_material_file(
                    json_source, raw_dir, [target + ".json" for target in targets])
            return written

        # Textures: the -s pass writes <name>.xbm.json, the plain pass <name>.png.
        first = run_command(
            [cli, "uncook", *archive_args, "-o", str(tmp), "-s",
             "--uext", "png", "-w", f"*{stem}*"],
            logger=log, timeout=UNCOOK_TIMEOUT,
        )
        # The second pass only needs the archives the first one actually hit.
        matched = {Path(name).name.lower() for name in _matches_in_output(first)}
        second = [arg for arg in archive_args if Path(arg).name.lower() in matched]
        run_command(
            [cli, "uncook", *(second or archive_args), "-o", str(tmp),
             "--uext", "png", "-w", f"*{stem}*"],
            logger=log, timeout=UNCOOK_TIMEOUT,
        )
        png_targets = [
            str(Path(target).with_suffix(".png")).replace("\\", "/")
            for target in targets
        ]
        source = _find_material_source(tmp, [vanilla_rel], _MATERIAL_TEXTURE_EXTS)
        if source is not None:
            written += _copy_material_file(source, raw_dir, targets)
            json_source = Path(str(source) + ".json")
            if json_source.is_file():
                written += _copy_material_file(
                    json_source, raw_dir, [target + ".json" for target in targets])
            png_source = source.with_suffix(".png")
            if png_source.is_file():
                written += _copy_material_file(png_source, raw_dir, png_targets)
            return written
        png_source = _find_material_source(tmp, png_targets, (".png",))
        if png_source is not None:
            written += _copy_material_file(png_source, raw_dir, png_targets)
        return written
    except OSError as exc:
        log.warn(f"  material heal failed ({vanilla_rel}): {exc}")
        return written
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _material_search_groups(game: str) -> list[list[Path]]:
    """Archive batches for the material heal, most likely sources first.

    ``extract`` prints one line per archive and scans every one it is given, so
    handing it 70+ archives makes a single lookup take ~10 s.  Character,
    material and texture resources sit in a handful of ``*appearance*`` /
    ``*gamedata*`` / ``*engine*`` archives, so those go in small batches first;
    language/audio/video packs are searched only as a last resort.  The mod
    folder is one entry because ``extract`` scans a folder in one call.
    """
    archives = _game_archives(game)
    priority = ("appearance", "gamedata", "engine", "nightcity", "characters")
    likely = [a for a in archives if any(k in a.name.lower() for k in priority)]
    rest = [a for a in archives if a not in likely]
    groups: list[list[Path]] = []
    for index in range(0, len(likely), 6):
        groups.append(likely[index:index + 6])
    mods = Path(game) / "archive" / "pc" / "mod"
    try:
        if mods.is_dir() and any(mods.glob("*.archive")):
            groups.append([mods])
    except OSError:
        pass
    for index in range(0, len(rest), 8):
        groups.append(rest[index:index + 8])
    return groups


def _material_ref_order(ref: str) -> int:
    """Heal order: material instances, then textures, then layer chains.

    ``0`` = ``.mi``, ``1`` = ``.xbm``/``.dds``, ``2`` = ``.mlsetup``/``.mlmask``,
    ``3`` = ``.mltemplate``.  Colour-bearing files must not be starved by the
    hundreds of surface templates a garment chain pulls in.
    """
    lower = ref.lower()
    if lower.endswith(_MATERIAL_SERIALIZE_EXTS):
        return 0
    if lower.endswith(_MATERIAL_TEXTURE_EXTS):
        return 1
    if lower.endswith(".mltemplate"):
        return 3
    return 2


def heal_material_dependencies(
    raw_dir: Path,
    settings: Settings,
    logger: object = None,
    time_budget: float = HEAL_TIME_BUDGET,
) -> dict:
    """Resolve the missing ``.mi``/``.mlsetup``/``.xbm`` chain of the cache.

    A normal uncook only fills the entity closure: ``Material.json`` still points
    at ``.mi`` files that are not in the tree (``uncook`` skips that type) and each
    ``.mi.json`` points at textures of its own, so the add-on substitutes
    black/grey/uvchecker placeholders and the bake reproduces those.  This walks
    the json refs, unbundles what is missing from the vanilla/mod archives and
    mirrors the result into the depot at **both** the vanilla and the
    ``base/acm_npv/<slug>/`` path (the add-on resolves the prefixed one).

    A few passes are needed because every healed ``.mi.json`` lists new textures.
    Never raises.
    """
    log = logger or _NULL
    info: dict = {
        "passes": [],
        "requested": 0,
        "healed": 0,
        "written": 0,
        "mi_healed": 0,
        "xbm_healed": 0,
        "missed": [],
        "warnings": [],
        "skipped": False,
    }
    raw_dir = Path(raw_dir)
    cli_value = (settings.wolvenkit_cli or "").strip()
    game = (settings.game_root or "").strip()
    if not cli_value or not Path(cli_value).is_file() or not game:
        info["skipped"] = True
        note = "Material dependency heal skipped: wolvenkit_cli/game_root not configured."
        log.warn(note)
        info["warnings"].append(note)
        return info
    cli = Path(cli_value)
    groups = _material_search_groups(game)
    if not groups:
        info["skipped"] = True
        note = "Material dependency heal skipped: no .archive files under archive\\pc."
        log.warn(note)
        info["warnings"].append(note)
        return info
    prefix = _npv_depot_prefix(raw_dir)
    deadline = time.monotonic() + max(60.0, float(time_budget))
    resolved: set[str] = set()
    missed: set[str] = set()

    for pass_no in range(1, HEAL_PASSES + 1):
        refs = _material_refs(raw_dir)
        missing: dict[str, str] = {}
        for ref in refs:
            if _material_present(raw_dir, ref):
                continue
            vanilla = _NPV_DEPOT_PREFIX.sub("", ref, count=1) or ref
            if vanilla in resolved or vanilla in missed:
                continue
            missing[vanilla] = ref
        if not missing:
            if pass_no == 1:
                log.info("Material dependencies: nothing missing in the cache.")
            break
        info["requested"] += len(missing)
        log.info(
            f"Material heal pass {pass_no}/{HEAL_PASSES}: {len(missing)} missing ref(s) "
            f"({len(refs)} scanned)..."
        )
        healed = mi = xbm = 0
        files_written = 0
        out_of_time = False
        for vanilla, ref in sorted(
            missing.items(), key=lambda item: (_material_ref_order(item[0]), item[0])
        ):
            if time.monotonic() > deadline:
                out_of_time = True
                note = "Material heal time budget reached; remaining refs stay placeholders."
                log.warn(f"  {note}")
                info["warnings"].append(note)
                break
            written = _mirror_material(raw_dir, vanilla, prefix)
            if written:
                ok = True
            else:
                ok = False
                for archives in groups:
                    try:
                        written = _heal_material_ref(cli, archives, vanilla, raw_dir, prefix, log)
                        if written:
                            ok = True
                            break
                    except Exception as exc:           # never crash the pipeline
                        log.warn(f"  material heal failed ({vanilla}): {exc}")
            if ok:
                healed += 1
                files_written += written
                resolved.add(vanilla)
                if Path(vanilla.lower()).suffix in _MATERIAL_SERIALIZE_EXTS:
                    mi += 1
                    log.info(f"  healed material: {Path(vanilla).name}")
                else:
                    xbm += 1
                    log.info(f"  healed texture: {Path(vanilla).name}")
            else:
                missed.add(vanilla)
        entry = {
            "pass": pass_no,
            "found": len(missing),
            "healed": healed,
            "mi": mi,
            "xbm": xbm,
            "missed": len(missing) - healed,
        }
        info["passes"].append(entry)
        info["healed"] += healed
        info["written"] += files_written
        info["mi_healed"] += mi
        info["xbm_healed"] += xbm
        log.info(
            f"  pass {pass_no}: healed {healed}/{len(missing)} "
            f"(.mi-family {mi}, textures {xbm}, unresolved {entry['missed']})"
        )
        if healed == 0 or out_of_time:
            break

    for ref in _material_refs(raw_dir):
        if _material_present(raw_dir, ref):
            continue
        vanilla = _NPV_DEPOT_PREFIX.sub("", ref, count=1) or ref
        if vanilla not in info["missed"]:
            info["missed"].append(vanilla)
    info["missed"].sort()
    if info["missed"]:
        log.warn(
            f"Material dependencies: {len(info['missed'])} ref(s) are not in this "
            "install; the add-on keeps its placeholder for those."
        )
    else:
        log.info("Material dependencies: complete.")
    return info


# ---------------------------------------------------------------------------
# texture png guarantee (the add-on resolves .xbm -> a sibling .png)
# ---------------------------------------------------------------------------

#: ``uncook -s`` writes the .xbm json only; the add-on needs the .png next to it.
_PNG_EXPORT_TIMEOUT = 600       # seconds per folder batch


def _walk_texture_refs(value: object, found: set) -> None:
    """Collect ``.xbm`` depot refs of a material json (templates skipped)."""
    if isinstance(value, dict):
        for item in value.values():
            _walk_texture_refs(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_texture_refs(item, found)
    elif isinstance(value, str):
        text = re.sub(r"/{2,}", "/", value.strip().replace("\\", "/")).lstrip("/*")
        if not text.lower().endswith(".xbm") or "/" not in text:
            return
        if any(ch in text for ch in "{}*?") or _ABSOLUTE_PATH.match(text):
            return                     # unresolved template / serialize metadata
        found.add(text)


def _texture_png_refs(raw_dir: Path) -> list[str]:
    """``.xbm`` refs of the cached material json files.

    ``.mltemplate.json`` holds the textile albedos (``colorTexture``) of every
    multilayer layer; those are the surface maps a garment needs, so they count
    just like the slots of a ``Material.json``.
    """
    found: set = set()
    try:
        paths = sorted(
            p for p in Path(raw_dir).rglob("*.json")
            if p.is_file() and p.name.lower().endswith(
                (".material.json", ".mi.json", ".mltemplate.json")
            )
        )
    except OSError:
        return []
    for path in paths:
        data = _read_json(path)
        if data is None:
            continue
        if path.name.lower().endswith(".material.json"):
            for entry in data.get("Materials") or []:
                if isinstance(entry, dict):
                    _walk_texture_refs(entry.get("Data"), found)
        else:
            _walk_texture_refs(data, found)
    return sorted(found)


def ensure_texture_pngs(raw_dir: Path, settings: Settings, logger: object = None) -> dict:
    """Write the missing ``.png`` sibling of every referenced cached ``.xbm``.

    A serialize-only uncook leaves ``<name>.xbm`` + ``<name>.xbm.json`` in the
    cache; the CP77 add-on resolves the depot ``.xbm`` to ``<name>.png`` and logs
    "texture file(s) could not be loaded" when that png is absent, so its bake
    reproduces the generated placeholder.  ``export`` on the cached .xbm writes
    the png directly (fast, no archive scan); a ref that fails there falls back
    to the usual ``-s`` + plain uncook pair for its stem.  Never raises.
    """
    log = logger or _NULL
    info: dict = {"checked": 0, "created": 0, "missing": [], "warnings": []}
    raw_dir = Path(raw_dir)
    cli_value = (settings.wolvenkit_cli or "").strip()
    game = (settings.game_root or "").strip()
    cli = Path(cli_value) if cli_value else None

    pending: dict[Path, list[Path]] = {}
    for ref in _texture_png_refs(raw_dir):
        local = _depot_to_local(raw_dir, ref)
        if not local.is_file():
            continue
        info["checked"] += 1
        if local.with_suffix(".png").is_file():
            continue
        pending.setdefault(local.parent, []).append(local)
    if not pending:
        log.info("Texture pngs: every referenced .xbm already has its .png sibling.")
        return info

    total = sum(len(items) for items in pending.values())
    log.step(f"Writing {total} missing texture png(s) from the cached .xbm files ...")
    tmp = WORK_ROOT / "_pngfix"
    groups = _material_search_groups(game) if game else []
    prefix = _npv_depot_prefix(raw_dir)

    for parent, sources in sorted(pending.items(), key=lambda item: str(item[0]).lower()):
        produced: dict[Path, Path] = {}
        if cli is not None and cli.is_file() and game:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                tmp.mkdir(parents=True, exist_ok=True)
                run_command(
                    [cli, "export", *[str(item) for item in sources], "-o", str(tmp),
                     "--uext", "png", "-gp", game],
                    logger=log, timeout=_PNG_EXPORT_TIMEOUT,
                )
            except Exception as exc:                   # never crash the pipeline
                log.warn(f"  png export failed for {parent.name}: {exc}")
            try:
                for item in sorted(tmp.glob("*.png")):
                    produced.setdefault(item.stem.lower(), item)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

        for source in sources:
            png = source.with_suffix(".png")
            found = produced.get(source.stem.lower())
            if found is not None:
                try:
                    shutil.copyfile(found, png)
                    info["created"] += 1
                    continue
                except OSError as exc:
                    log.warn(f"  could not write {png.name}: {exc}")
            # Fallback: the usual dual uncook of this .xbm's stem.
            vanilla = _vanilla_rel_path(source, raw_dir)
            if vanilla and groups and cli is not None and cli.is_file():
                for archives in groups:
                    try:
                        if _heal_material_ref(cli, archives, vanilla, raw_dir, prefix, log):
                            break
                    except Exception as exc:           # never crash the pipeline
                        log.warn(f"  png heal failed ({vanilla}): {exc}")
            if png.is_file():
                info["created"] += 1
                continue
            rel = str(Path(vanilla or source.name)).replace("\\", "/")
            if rel not in info["missing"]:
                info["missing"].append(rel)

    info["missing"].sort()
    if info["missing"]:
        note = (
            f"Texture pngs: {len(info['missing'])} .xbm file(s) still have no .png; the "
            "add-on will use a generated placeholder for those."
        )
        log.warn(note)
        info["warnings"].append(note)
    log.info(
        f"Texture pngs: {info['created']} created, {len(info['missing'])} still missing "
        f"({info['checked']} .xbm ref(s) checked)."
    )
    return info


def convert_meshes_to_glb(
    npv_name: str,
    settings: Settings,
    logger: object = None,
    force: bool = False,
) -> dict:
    """Export every cached .mesh of ``npv_name`` to a sibling .glb.

    The CP77 Blender add-on only loads mesh geometry from .glb files, so the
    uncook tree is turned into a glb tree before Blender runs.  The depot
    (MaterialRepo) is patched to this NPV's own ``work/<npv>/source/raw`` so a
    second NPV export cannot mix caches.  Failures are collected as warnings;
    this function never raises.
    """
    log = logger or _NULL
    info: dict = {
        "ok": False,
        "raw_dir": None,
        "meshes": 0,
        "exported": 0,
        "skipped": 0,
        "failed": 0,
        "healed": 0,
        "depot": None,
        "warnings": [],
    }
    raw_dir = raw_dir_for(npv_name)
    info["raw_dir"] = str(raw_dir)
    cli_value = (settings.wolvenkit_cli or "").strip()
    game = (settings.game_root or "").strip()
    if not cli_value or not Path(cli_value).is_file():
        note = "Mesh->GLB conversion skipped: wolvenkit_cli is not configured."
        log.warn(note)
        info["warnings"].append(note)
        return info
    if not game:
        note = "Mesh->GLB conversion skipped: game_root is not configured (WolvenKit needs -gp)."
        log.warn(note)
        info["warnings"].append(note)
        return info
    cli = Path(cli_value)
    meshes = _meshes(raw_dir)
    info["meshes"] = len(meshes)
    if not meshes:
        note = f"Mesh->GLB conversion skipped: no .mesh files under {raw_dir}."
        log.warn(note)
        info["warnings"].append(note)
        return info

    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        note = f"Mesh->GLB conversion skipped: cannot create {raw_dir} ({exc})."
        log.warn(note)
        info["warnings"].append(note)
        return info
    patched = patch_mesh_export_args(cli, raw_dir, log)
    info["depot"] = patched.get("path")
    if not patched.get("ok"):
        info["warnings"].append(
            "Could not set MeshExportArgs.MaterialRepo in appsettings.json "
            f"({patched.get('error')}); WolvenKit may refuse to export meshes with materials."
        )

    pending = [mesh for mesh in meshes if force or not _glb_for(mesh).is_file()]
    info["skipped"] = len(meshes) - len(pending)
    if info["skipped"]:
        log.info(f"{info['skipped']} .mesh file(s) already have a .glb - skipped.")
    if pending:
        log.step(f"Converting {len(pending)} mesh(es) to glb (materials into {raw_dir}) ...")
    for index, mesh in enumerate(pending, start=1):
        cmd = [
            cli, "export", str(mesh),
            "-o", str(mesh.parent),
            "-gp", game,
        ]
        try:
            result = run_command(cmd, logger=log, timeout=MESH_EXPORT_TIMEOUT)
        except Exception as exc:                      # never crash the pipeline
            result = CommandResult(code=1, lines=[f"{type(exc).__name__}: {exc}"])
        glb = _glb_for(mesh)
        if glb.is_file() and glb.stat().st_size > 0:
            info["exported"] += 1
            log.info(f"  [{index}/{len(pending)}] glb: {glb.name}")
            continue
        # A truncated NPV copy cannot be exported; heal it from the archive copy of
        # the same depot path and use the glb that copy produces.
        if _heal_from_archives(mesh, raw_dir, settings, log) and _is_valid_glb(glb):
            info["healed"] += 1
            info["exported"] += 1
            log.info(f"  [{index}/{len(pending)}] glb (healed): {glb.name}")
            continue
        info["failed"] += 1
        note = f"{mesh.name}: no glb produced (exit {result.code})"
        log.warn(f"  [{index}/{len(pending)}] {note}")
        info["warnings"].append(note + " - " + _tail(result.output, 6))
    info["ok"] = info["failed"] == 0 and (info["exported"] + info["skipped"]) > 0
    log.info(
        f"Mesh->GLB: {info['exported']} exported, {info['skipped']} cached, "
        f"{info['failed']} failed of {info['meshes']} mesh(es)."
    )
    return info


def scan_npv(npv: NpvInfo, settings: Settings, logger: object = None, force: bool = False) -> LookScan:
    raw = uncook_npv(npv, settings, logger, force=force)
    return discover_looks(raw, npv.name, logger)


# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------

def probe_blender(blender: Path, logger: object = None, refresh: bool = False) -> dict:
    """Return {'ok', 'version', 'output'} for a blender.exe; memoised per path."""
    log = logger or _NULL
    global _BLENDER_PROBE_CACHE
    key = str(blender)
    try:
        mtime = Path(blender).stat().st_mtime
    except OSError:
        mtime = 0
    cached = _BLENDER_PROBE_CACHE.get(key)
    if cached and cached.get("mtime") == mtime and not refresh:
        return cached["info"]

    info = {"ok": False, "version": None, "version_string": "", "output": ""}
    result = run_command([blender, "--version"], logger=log, timeout=180)
    info["output"] = result.output
    match = re.search(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?", result.output)
    if match:
        info["version"] = (int(match.group(1)), int(match.group(2)))
        info["version_string"] = match.group(0)
        info["ok"] = result.code == 0
    elif result.code == 0:
        info["ok"] = True
    _BLENDER_PROBE_CACHE[key] = {"mtime": mtime, "info": info}
    return info


_BLENDER_PROBE_CACHE: dict[str, dict] = {}


_ADDON_PROBE_EXPR = (
    "import bpy, addon_utils, json\n"
    "names = [m.__name__ for m in addon_utils.modules() if 'cp77' in m.__name__.lower()]\n"
    "payload = {'version': bpy.app.version_string, 'addons': names,\n"
    "           'enabled': [n for n in names if addon_utils.check(n)[1]]}\n"
    "print('##VEXPORT_PROBE## ' + json.dumps(payload))\n"
)


def probe_blender_addons(blender: Path, logger: object = None) -> dict:
    log = logger or _NULL
    result = run_command(
        [blender, "--background", "--python-expr", _ADDON_PROBE_EXPR],
        logger=log,
        timeout=240,
    )
    payload: dict = {}
    for line in result.lines:
        marker = line.find("##VEXPORT_PROBE##")
        if marker >= 0:
            try:
                payload = json.loads(line[marker + len("##VEXPORT_PROBE##"):].strip())
            except ValueError:
                payload = {}
            break
    return {"ok": bool(payload), "code": result.code, "payload": payload, "output": result.output}


def blender_export(
    blender: Path,
    raw: Path,
    ent: Path,
    npv_name: str,
    look: str,
    out_dir: Path,
    logger: object = None,
    timeout: float = BLENDER_TIMEOUT,
    render_proofs: bool = True,
) -> dict:
    log = logger or _NULL
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        blender,
        "--background",
        "--python-exit-code", "1",
        "--python", BLENDER_SCRIPT,
        "--",
        "--raw", raw,
        "--ent", ent,
        "--look", look,
        "--out", out_dir,
        "--npv", npv_name,
    ]
    if not render_proofs:
        cmd.append("--no-proofs")
    result = run_command(cmd, logger=log, timeout=timeout)
    report = None
    marker = "##VEXPORT_RESULT##"
    for line in result.lines:
        index = line.find(marker)
        if index >= 0:
            try:
                report = json.loads(line[index + len(marker):].strip())
            except ValueError:
                report = None
    if report is None:
        log.error(
            "Blender did not return a result block. See the output above for the Python traceback."
        )
    return {"code": result.code, "report": report, "output": result.output}


def ensure_blender(blender: Path, logger: object = None) -> dict:
    log = logger or _NULL
    if not Path(blender).is_file():
        raise SetupError(
            f"blender.exe was not found at:\n{blender}\n\n"
            "Install Blender 5.0+ (the Cyberpunk Blender IO Suite 2.5.0 requires it) from "
            "https://www.blender.org/download/ and point the Blender row at blender.exe."
        )
    info = probe_blender(Path(blender), log)
    if not info["ok"]:
        raise SetupError(
            f"Blender could not be started:\n{blender}\n\n" + _tail(info["output"], 15)
        )
    version = info.get("version")
    if version and version < (4, 0):
        raise SetupError(
            f"Blender {info['version_string']} is too old. OBJ export requires Blender 4.0+; "
            "the Cyberpunk Blender IO Suite 2.5.0 requires Blender 5.0."
        )
    if version and version < (5, 0):
        log.warn(
            f"Blender {info['version_string']} detected. Add-on 2.5.0 officially requires Blender 5.0; "
            "import may fail; install Blender 5.x if it does."
        )
    return info


# ---------------------------------------------------------------------------
# export orchestration
# ---------------------------------------------------------------------------

def _pick_entity_override(settings: Settings, npv_name: str, scan: LookScan) -> Optional[Path]:
    override = (settings.entity_overrides or {}).get(npv_name, "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
    return scan.ent_json


def export_npv(
    npv: NpvInfo,
    requested_looks: Sequence[str],
    settings: Settings,
    logger: object = None,
    force_uncook: bool = False,
) -> dict:
    """Uncook + export one NPV. Returns a summary dict; raises PipelineError."""
    log = logger or _NULL
    if not npv.exportable:
        raise SetupError(
            f"{npv.name}: {draft_message(npv)}\n\nDraft folder:\n{npv.directory}"
        )
    blender_value = (settings.blender or "").strip()
    if not blender_value:
        raise SetupError(
            "blender.exe is not configured.\n\n"
            "Install Blender 5.0+ from https://www.blender.org/download/, then use 'Browse' in the "
            "Blender row."
        )
    blender = Path(blender_value)
    ensure_blender(blender, log)

    output_value = (settings.output_root or "").strip()
    if not output_value:
        raise SetupError("The output folder is not configured. Set it in the settings row.")
    output_root = Path(output_value)

    raw = uncook_npv(npv, settings, log, force=force_uncook)
    scan = discover_looks(raw, npv.name, log)
    ent = _pick_entity_override(settings, npv.name, scan)
    if ent is None:
        raise PipelineError(
            f"{npv.name}: no *.ent.json was found under {raw}.\n"
            "The archive may not contain character entity data, or uncooking was incomplete."
        )
    if ent != scan.ent_json:
        log.info(f"Using entity override for {npv.name}: {ent}")

    deps = uncook_dependencies(raw, ent, settings, log)
    # The material graphs (Material.json -> .mi -> its textures) are not part of the
    # entity closure; without them the add-on substitutes uvchecker placeholders.
    materials = heal_material_dependencies(raw, settings, log)
    # The CP77 add-on loads mesh geometry only from .glb files, so finish the cache
    # with the per-mesh WolvenKit "export" command (it also fills the material depot
    # through MeshExportArgs.MaterialRepo - see convert_meshes_to_glb).  A heal that
    # wrote files means the cached .glb carry stale Material.json: force the redo.
    deps_written = materials.get("written", materials.get("healed", 0)) or 0
    meshes = convert_meshes_to_glb(npv.name, settings, log, force=deps_written > 0)
    # The exported Material.json still hold engine placeholder slots; point them at
    # the .mi textures before the add-on builds (and bakes) the materials.
    remap = remap_material_jsons(raw, log)
    # A serialize-only uncook leaves textures as .xbm without the .png the add-on
    # resolves them to; write the missing siblings before Blender loads them.
    pngs = ensure_texture_pngs(raw, settings, log)

    looks = [look for look in requested_looks if look and look != "ALL"]
    if not looks:
        looks = list(scan.looks)
    if not looks:
        looks = ["ALL"]
        log.warn(
            f"{npv.name}: no look names known; importing ALL appearances in one Blender run and "
            "exporting each appearance collection separately."
        )

    try:
        out_dir = output_root / safe_filename(npv.name, fallback="npv")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "textures").mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PipelineError(f"Output folder is not writable: {output_root} ({exc})") from exc

    results: list[dict] = []
    for index, look in enumerate(looks, start=1):
        log.step(f"[{npv.name}] Look {index}/{len(looks)}: {look}")
        run = blender_export(
            blender, raw, ent, npv.name, look, out_dir, log,
            render_proofs=settings.render_proofs,
        )
        report = run.get("report")
        entry = {
            "look": look,
            "exit_code": run["code"],
            "report": report,
            "ok": bool(report and report.get("ok")),
        }
        if report:
            if report.get("materials_fallback"):
                log.warn(
                    f"[{npv.name}/{look}] Materials could not be built headless; geometry was exported "
                    "WITHOUT materials. See README.md and EXPORT-RESEARCH.md."
                )
            if report.get("ok") is False:
                log.error(f"[{npv.name}/{look}] Blender reported a failure.")
            exported = report.get("looks") or {}
            for look_name, data in exported.items():
                files = data.get("files") or {}
                if files:
                    log.info(
                        f"[{npv.name}/{look_name}] exported: "
                        + ", ".join(f"{fmt.upper()} → {Path(path).name}" for fmt, path in files.items())
                    )
                proof = data.get("proof")
                if proof:
                    log.info(f"[{npv.name}/{look_name}] proof: {Path(proof).name}")
                elif data.get("proof_error"):
                    log.warn(f"[{npv.name}/{look_name}] proof failed: {data['proof_error']}")
                for error in data.get("errors") or []:
                    log.warn(f"[{npv.name}/{look_name}] {error}")
        else:
            log.error(f"[{npv.name}/{look}] Blender failed (exit code {run['code']}).")
        results.append(entry)

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "npv": npv.name,
        "archive": str(npv.primary_archive) if npv.primary_archive else None,
        "entity_json": str(ent),
        "raw_dir": str(raw),
        "output_dir": str(out_dir),
        "shared_textures_dir": str(out_dir / "textures"),
        "render_proofs": bool(settings.render_proofs),
        "requested_looks": list(looks),
        "discovered_looks": list(scan.looks),
        "dependencies": deps,
        "materials": materials,
        "materials_remap": remap,
        "textures": pngs,
        "meshes": meshes,
        "results": results,
        "log_file": str(LOG_PATH),
    }
    try:
        summary_path = out_dir / "export-summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info(f"Summary written: {summary_path}")
    except OSError as exc:
        log.warn(f"Could not write export-summary.json: {exc}")

    failures = sum(1 for entry in results if not entry["ok"])
    if failures:
        log.warn(f"[{npv.name}] {failures}/{len(results)} Blender run(s) reported failure.")
    else:
        log.info(f"[{npv.name}] Export complete: {out_dir}")
    return summary


# ---------------------------------------------------------------------------
# settings validation / selftest
# ---------------------------------------------------------------------------

def validate_settings(settings: Settings) -> list[str]:
    problems: list[str] = []
    cli = (settings.wolvenkit_cli or "").strip()
    if not cli:
        problems.append(
            "WolvenKit.CLI.exe is not set.\n"
            "Download WolvenKit.Console-<version>.zip (10.0+ required for game 2.5) from\n"
            "https://github.com/WolvenKit/WolvenKit/releases and pick the extracted "
            "WolvenKit.CLI.exe."
        )
    elif not Path(cli).is_file():
        problems.append(f"WolvenKit.CLI.exe does not exist at:\n{cli}")

    blender = (settings.blender or "").strip()
    if not blender:
        problems.append(
            "blender.exe is not set.\n"
            "Install Blender 5.0+ from https://www.blender.org/download/ and pick blender.exe."
        )
    elif not Path(blender).is_file():
        problems.append(f"blender.exe does not exist at:\n{blender}")

    npv_root = effective_npv_root(settings)
    if npv_root is None:
        problems.append(
            "The ACM NPV folder could not be located.\n"
            "Set the Cyberpunk 2077 game folder (the one containing bin\\x64\\Cyberpunk2077.exe) "
            "or browse directly to\n"
            "<game>\\bin\\x64\\plugins\\cyber_engine_tweaks\\mods\\AppearanceCreatorMod\\NPV"
        )

    output = (settings.output_root or "").strip()
    if not output:
        problems.append("The output folder is not set.")
    else:
        ancestor = Path(output)
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            problems.append(f"Output folder parent does not exist: {ancestor}")

    if not BLENDER_SCRIPT.is_file():
        problems.append(f"blender_export.py is missing next to v_exporter.py:\n{BLENDER_SCRIPT}")
    return problems


def _selftest_line(logger: object, status: str, label: str, detail: str = "") -> None:
    logger.info(f"[{status:<4}] {label}" + (f": {detail}" if detail else ""))


def _console_echo(_level: str, message: str) -> None:
    """print() that cannot crash on legacy Windows console code pages."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or "utf-8"
            print(str(message).encode(encoding, "replace").decode(encoding), flush=True)
        except Exception:
            pass


def run_selftest(settings: Optional[Settings] = None, logger: object = None) -> int:
    """Validate paths and dependencies; does NOT export anything."""
    global _last_settings_error
    log = logger or Logger(echo=_console_echo)
    owns_logger = logger is None

    log.step(f"{APP_TITLE} self-test")
    log.info(f"Tool folder : {APP_DIR}")
    log.info(f"Python      : {sys.version.split()[0]} ({sys.executable})")
    log.info(f"Platform    : {sys.platform}")
    log.info(f"Settings    : {SETTINGS_PATH}")
    log.info(f"Log file    : {LOG_PATH}")

    failures = 0
    warnings = 0

    if sys.version_info < (3, 9):
        failures += 1
        _selftest_line(log, "FAIL", "Python", "3.9+ is required.")
    else:
        _selftest_line(log, "OK", "Python", sys.version.split()[0])

    try:
        import tkinter  # noqa: F401,PLC0415

        _selftest_line(log, "OK", "tkinter", f"Tk {tkinter.TkVersion}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        _selftest_line(log, "FAIL", "tkinter", f"unavailable ({exc}) - the GUI cannot start.")

    created = False
    if settings is None:
        if not SETTINGS_PATH.is_file():
            log.info("settings.json not found; creating it with auto-detected values.")
            settings = blank_settings()
            detected_game = detect_game_root(log)
            if detected_game:
                settings.game_root = str(detected_game)
                npv_root = derive_npv_root(detected_game)
                if npv_root is not None:
                    settings.npv_root = str(npv_root)
            detected_blender = find_blender(log)
            if detected_blender:
                settings.blender = str(detected_blender)
            detected_cli = find_wolvenkit(log)
            if detected_cli:
                settings.wolvenkit_cli = str(detected_cli)
            save_settings(settings, logger=log)
            created = True
        else:
            settings = load_settings(logger=log)
    if _last_settings_error:
        warnings += 1
        _selftest_line(log, "WARN", "settings.json", _last_settings_error + " (a .bad backup was kept)")
    else:
        _selftest_line(log, "OK", "settings.json", "created" if created else "loaded")

    # WolvenKit CLI ---------------------------------------------------------
    cli_text = (settings.wolvenkit_cli or "").strip()
    if not cli_text:
        failures += 1
        _selftest_line(
            log, "FAIL", "WolvenKit CLI",
            "not configured. Download WolvenKit.Console-<ver>.zip from "
            "https://github.com/WolvenKit/WolvenKit/releases (10.0+ for game 2.5) and set it in the GUI.",
        )
    elif not Path(cli_text).is_file():
        failures += 1
        _selftest_line(log, "FAIL", "WolvenKit CLI", f"missing file: {cli_text}")
    else:
        _selftest_line(log, "OK", "WolvenKit CLI", cli_text)
        wk = probe_wolvenkit(Path(cli_text), log, refresh=True)
        if wk["ok"]:
            first = next((line for line in wk["output"].splitlines() if line.strip()), "started")
            _selftest_line(log, "OK", "WolvenKit run", first[:120])
        else:
            warnings += 1
            _selftest_line(
                log, "WARN", "WolvenKit run",
                "the CLI did not start cleanly (WolvenKit 10.x needs the .NET 10 runtime): "
                + (_tail(wk["output"], 3) or f"exit {wk['code']}"),
            )
        version = wk.get("version")
        if version is None:
            warnings += 1
            _selftest_line(
                log, "WARN", "WolvenKit version",
                "could not be parsed from the --version output (expected e.g. 10.0.0).",
            )
        elif version[0] < 10:
            warnings += 1
            _selftest_line(
                log, "WARN", "WolvenKit version",
                f"{version_text(version)} - pre-2.5 build; see the WARNING above / README.md.",
            )
        else:
            _selftest_line(log, "OK", "WolvenKit version", version_text(version))

    # Blender ---------------------------------------------------------------
    blender_text = (settings.blender or "").strip()
    if not blender_text:
        failures += 1
        _selftest_line(
            log, "FAIL", "Blender",
            "not configured. Install Blender 5.0+ from https://www.blender.org/download/ "
            "and set blender.exe in the GUI.",
        )
    elif not Path(blender_text).is_file():
        failures += 1
        _selftest_line(log, "FAIL", "Blender", f"missing file: {blender_text}")
    else:
        info = probe_blender(Path(blender_text), log, refresh=True)
        if not info["ok"]:
            failures += 1
            _selftest_line(log, "FAIL", "Blender", "could not be started: " + (_tail(info["output"], 2) or "?"))
        elif info.get("version") and info["version"] < (5, 0):
            warnings += 1
            _selftest_line(
                log, "WARN", "Blender",
                f"{info['version_string']} - add-on 2.5.0 requires Blender 5.0; imports may fail.",
            )
            probe = probe_blender_addons(Path(blender_text), log)
            payload = probe.get("payload") or {}
            modules = payload.get("addons") or []
            if modules:
                _selftest_line(log, "OK", "CP77 add-on", ", ".join(modules))
            else:
                failures += 1
                _selftest_line(
                    log, "FAIL", "CP77 add-on",
                    "Cyberpunk Blender IO Suite not found. Install the 2.5.0 add-on zip via "
                    "Edit > Preferences > Add-ons > Install from Disk.",
                )
        else:
            _selftest_line(log, "OK", "Blender", info.get("version_string") or "started")
            probe = probe_blender_addons(Path(blender_text), log)
            payload = probe.get("payload") or {}
            modules = payload.get("addons") or []
            enabled = payload.get("enabled") or []
            if modules:
                detail = ", ".join(modules)
                if modules[0] not in enabled:
                    warnings += 1
                    _selftest_line(
                        log, "WARN", "CP77 add-on",
                        f"{detail} installed but not enabled - enable it in Preferences > Add-ons.",
                    )
                else:
                    _selftest_line(log, "OK", "CP77 add-on", detail)
            else:
                failures += 1
                _selftest_line(
                    log, "FAIL", "CP77 add-on",
                    "not installed. Add-on 2.5.0: https://github.com/WolvenKit/Cyberpunk-Blender-add-on/releases "
                    "-> Edit > Preferences > Add-ons > Install from Disk.",
                )

    # Game / ACM ------------------------------------------------------------
    game = normalize_game_root(settings.game_root)
    if game is None:
        failures += 1
        _selftest_line(
            log, "FAIL", "Game folder",
            f"not valid: {settings.game_root or '(not set)'} - point it at the folder containing "
            "bin\\x64\\Cyberpunk2077.exe.",
        )
    else:
        _selftest_line(log, "OK", "Game folder", str(game))

    npv_root = effective_npv_root(settings, log)
    if npv_root is None:
        failures += 1
        acm = acm_mod_dir(game) if game is not None else None
        if acm is not None:
            expected = derive_npv_root(game) or (acm / "NPV")
            _selftest_line(
                log, "FAIL", "ACM NPV folder",
                f"ACM is installed at {acm}, but no NPV folder exists yet ({expected}). "
                "In game: open ACM, target V, save your look(s), then click 'Create NPV'.",
            )
        else:
            _selftest_line(
                log, "FAIL", "ACM NPV folder",
                "AppearanceCreatorMod was not found under "
                f"{_mods_dir_for_game(game) if game else '<game>'}. Install ACM 2.71+ "
                "(Nexus mod #10795) and click 'Create NPV' in game.",
            )
    else:
        npvs = list_npvs(npv_root, log)
        packed = [npv for npv in npvs if npv.exportable]
        drafts = [npv for npv in npvs if npv.is_draft]
        summary = f"{npv_root} ({len(packed)} packed, {len(drafts)} draft)"
        _selftest_line(log, "OK", "ACM NPV folder", summary)
        for npv in npvs:
            if npv.is_draft:
                warnings += 1
                _selftest_line(log, "WARN", f"  NPV {npv.name} (DRAFT)", draft_message(npv))
            else:
                detail = ", ".join(a.name for a in npv.archives) if npv.archives else "NO .archive"
                _selftest_line(log, "INFO", f"  NPV {npv.name}", detail)
        if not npvs:
            warnings += 1
            _selftest_line(
                log, "WARN", "NPV list",
                "no NPV folders yet. In game: ACM → target V → Save Look(s) → Create NPV.",
            )
        for npv in npvs:
            cached = raw_dir_for(npv.name)
            if cached.is_dir():
                try:
                    if any(cached.iterdir()):
                        _selftest_line(log, "INFO", f"  cache {npv.name}", str(cached))
                except OSError:
                    pass

    # Output ---------------------------------------------------------------
    output_text = (settings.output_root or "").strip()
    if not output_text:
        failures += 1
        _selftest_line(log, "FAIL", "Output folder", "not set.")
    else:
        output_path = Path(output_text)
        ancestor = output_path
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if ancestor.is_dir():
            writable = os.access(ancestor, os.W_OK)
            detail = str(output_path)
            if not output_path.exists():
                detail += " (created on first export)"
            if writable:
                _selftest_line(log, "OK", "Output folder", detail)
            else:
                warnings += 1
                _selftest_line(log, "WARN", "Output folder", detail + " - nearest existing folder looks read-only.")
        else:
            failures += 1
            _selftest_line(log, "FAIL", "Output folder", f"parent missing: {ancestor}")

    # Helper script --------------------------------------------------------
    if BLENDER_SCRIPT.is_file():
        import py_compile  # noqa: PLC0415

        try:
            py_compile.compile(str(BLENDER_SCRIPT), doraise=True)
            _selftest_line(log, "OK", "blender_export.py", "present and compiles")
        except py_compile.PyCompileError as exc:
            failures += 1
            _selftest_line(log, "FAIL", "blender_export.py", f"does not compile: {exc}")
    else:
        failures += 1
        _selftest_line(log, "FAIL", "blender_export.py", f"missing: {BLENDER_SCRIPT}")

    if failures:
        log.warn(
            f"SELF-TEST: setup incomplete - {failures} issue(s), {warnings} warning(s). "
            "Fix the FAIL lines above; see README.md."
        )
    elif warnings:
        log.info(f"SELF-TEST: ready with {warnings} warning(s).")
    else:
        log.info("SELF-TEST: ready to export.")
    log.info("No export was run (self-test only).")

    if owns_logger:
        log.close()
    return 1 if failures else 0
