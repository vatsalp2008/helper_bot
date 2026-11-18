"""Several fills side by side (Vatsal, 2026-11-15): each takes its own browser profile slot."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from helper.browser import claim_profile, release_profile
from helper.errors import HelperError


def test_each_fill_gets_its_own_profile(tmp_path: Path) -> None:
    base = tmp_path / "browser-profile"
    first = claim_profile(base)
    second = claim_profile(base)
    assert (first, second) == (base, tmp_path / "browser-profile-2")
    release_profile(first)
    assert claim_profile(base) == base  # a released slot is free again
    with pytest.raises(HelperError, match="already open"):
        for _ in range(6):
            claim_profile(base)


def test_locks_left_by_a_crashed_fill_are_cleared(tmp_path: Path) -> None:
    base = tmp_path / "browser-profile"
    base.mkdir()
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    pid = dead.stdout.strip()  # that process has ended
    os.symlink(f"{socket.gethostname()}-{pid}", base / "SingletonLock")
    (base / "helper-fill.pid").write_text(pid)
    assert claim_profile(base) == base
    assert not (base / "SingletonLock").is_symlink()


def test_a_profile_chrome_has_open_is_skipped(tmp_path: Path) -> None:
    base = tmp_path / "browser-profile"
    base.mkdir()
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", base / "SingletonLock")  # a live process holds it
    assert claim_profile(base) == tmp_path / "browser-profile-2"


def test_no_window_limit_when_slots_is_zero(tmp_path: Path) -> None:  # the job hunt, 2026-11-16
    base = tmp_path / "browser-profile"
    claimed = [claim_profile(base, 0) for _ in range(9)]  # more than the old limit of 6
    assert len(set(claimed)) == 9 and claimed[-1] == tmp_path / "browser-profile-9"


def test_windows_keep_running_when_hidden() -> None:  # unattended evening timeouts, 2026-11-17
    from helper.browser import KEEP_ACTIVE_ARGS

    assert "--disable-backgrounding-occluded-windows" in KEEP_ACTIVE_ARGS
    assert "--disable-renderer-backgrounding" in KEEP_ACTIVE_ARGS
    assert not any("automation" in a.lower() or "user-agent" in a.lower() for a in KEEP_ACTIVE_ARGS)  # no disguise
