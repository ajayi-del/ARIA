"""Pins for the single-instance pidfile guard (pidfile-truncate-empty-pid-0924).

Defect: `open(lock_path, "w")` truncated the pidfile BEFORE the flock attempt,
so a challenger wiped the holder's PID and the refusal read an empty file:
"[ARIA] Another instance is already running (PID ). Kill it first: kill".
Observed live on the operator pane 2026-09-23/24 (two consecutive cycles).
Fix: open "a+", read via the locked handle, truncate only after acquiring.
"""

import fcntl
import os

import main


def _hold_lock(path, pid="424242"):
    fh = open(path, "w")
    fh.write(pid)
    fh.flush()
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_refusal_names_holder_pid_and_preserves_file(tmp_path, capsys):
    """Challenger on a held lock: refusal names the holder PID, and the
    holder's pidfile content is NOT truncated by the attempt."""
    lock = tmp_path / "aria.pid"
    holder = _hold_lock(lock, pid="424242")
    try:
        assert main._single_instance_lock(lock) is None
        err = capsys.readouterr().err
        assert "PID 424242" in err
        assert "kill 424242" in err
        assert lock.read_text() == "424242"
    finally:
        holder.close()


def test_acquires_when_free_and_writes_own_pid(tmp_path):
    """Free lock: acquisition succeeds and the pidfile carries our PID."""
    lock = tmp_path / "aria.pid"
    fh = main._single_instance_lock(lock)
    try:
        assert fh is not None
        assert lock.read_text() == str(os.getpid())
    finally:
        fh.close()


def test_acquires_over_stale_pidfile(tmp_path):
    """Stale content without a live flock: acquisition overwrites with our
    PID (a dead holder's lock is released at process death)."""
    lock = tmp_path / "aria.pid"
    lock.write_text("999999")
    fh = main._single_instance_lock(lock)
    try:
        assert fh is not None
        assert lock.read_text() == str(os.getpid())
    finally:
        fh.close()


def test_second_process_style_conflict_after_holder_close(tmp_path):
    """After the holder closes (process exit), a new challenger acquires —
    the restart path the broken guard was blocking."""
    lock = tmp_path / "aria.pid"
    holder = _hold_lock(lock, pid="424242")
    holder.close()
    fh = main._single_instance_lock(lock)
    try:
        assert fh is not None
        assert lock.read_text() == str(os.getpid())
    finally:
        fh.close()
