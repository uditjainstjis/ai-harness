"""Pramana as a Mac app: the Studio in its own native window (WebKit), no terminal and no browser needed.

Built into Pramana.app by packaging/macos/build_dmg.sh. Differences from `make run`:
- the API key is pasted into the app once and kept in ~/Library/Application Support/Pramana (0600),
  because an app opened from Finder has no shell to export AI_API_KEY from (an exported key still wins);
- runs and cloned repositories live in that same folder, not inside the read-only app bundle;
- the PATH of the user's login shell is adopted, so git, gh, uv and node are found as in Terminal.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home() / "Library" / "Application Support" / "Pramana"


def _adopt_login_path() -> None:
    """Apps launched from Finder get /usr/bin:/bin:/usr/sbin:/sbin only."""
    login = ""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        login = subprocess.run([shell, "-lc", 'printf %s "$PATH"'], capture_output=True, text=True, timeout=8).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), str(Path.home() / ".cargo/bin")]
    parts = (login.split(":") if login else []) + os.environ.get("PATH", "").split(":") + extra
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    os.environ["PATH"] = ":".join(seen)


def main() -> int:
    os.environ["PRAMANA_APP"] = "1"
    _adopt_login_path()
    HOME.mkdir(parents=True, exist_ok=True)
    os.chdir(HOME)
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    if (bundle / "pramana.toml").is_file():
        os.environ.setdefault("PRAMANA_CONFIG", str(bundle / "pramana.toml"))
    overrides = {"paths": {"runs_dir": str(HOME / "runs"), "workspace_dir": str(HOME / "workspace")}}

    from .server import start

    url, studio, httpd = start(port=8765, overrides=overrides)
    import webview

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window("Pramana Studio", url, width=1320, height=900, min_size=(960, 640))
    webview.start()
    try:                                   # the window is gone: stop every run and command it started
        studio.stop_all()
    except Exception:  # noqa: BLE001
        pass
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
