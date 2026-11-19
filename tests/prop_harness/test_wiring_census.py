"""wiring_census pins — real-tree schema + synthetic fixture verdicts.

Pin (d) records the 2026-11-17 ground truth for basket_tp.json: the
literal appears NOWHERE in prop_harness (writer AND readers removed;
only a docstring mention of ``harvest_basket_tp`` survives in
executor.py), so no static literal scan can report it — the ghost file
exists only on disk in the state root, outside the tree. If a future
change reintroduces the literal (e.g. a reader without a writer), the
census MUST start flagging it and this pin must be re-encoded.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from prop_harness.tools import wiring_census as wc

REPO = Path(__file__).resolve().parents[2]
PROP = REPO / "prop_harness"


# ------------------------------------------------------------ fixtures

_CONFIG = '''"""Synthetic config mirroring the PropConfig.from_env idiom."""
import os
from dataclasses import dataclass


@dataclass
class PropConfig:
    wired_knob: float = 1.0
    orphan_knob: float = 2.0

    @classmethod
    def from_env(cls) -> "PropConfig":
        def f(name: str, default: float) -> float:
            raw = os.environ.get(f"PROP_{name}", "")
            return float(raw) if raw else default

        return cls(
            wired_knob=f("WIRED_KNOB", 1.0),
            orphan_knob=f("ORPHAN_KNOB", 2.0),
        )
'''

_WORKER_HEAD = '''"""Synthetic worker."""
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write_json(path: Path, obj) -> None:
    path.write_text(__import__("json").dumps(obj))


def read_json(path: Path):
    import json
    return json.loads(path.read_text())


def run(cfg, root: Path) -> None:
'''

_FIXTURE_CASES = {
    "leaky": {
        "worker_body": '''    log.info("dead_event", n=1)
    log.info("used_event", n=1)
    atomic_write_json(root / "ghost.json", {"x": 1})
    atomic_write_json(root / "wired.json", {"x": 1})
    _ = read_json(root / "wired.json")
    return cfg.wired_knob
''',
        "consumer": 'USED = "used_event"\n',
    },
    "clean": {
        "worker_body": '''    log.info("used_event", n=1)
    atomic_write_json(root / "wired.json", {"x": 1})
    _ = read_json(root / "wired.json")
    return cfg.wired_knob + cfg.orphan_knob
''',
        "consumer": 'USED = "used_event"\n',
    },
}


def _fixture(tmp_path: Path, case: str) -> tuple[Path, Path]:
    spec = _FIXTURE_CASES[case]
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "config.py").write_text(_CONFIG)
    (pkg / "worker.py").write_text(_WORKER_HEAD + spec["worker_body"])
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_consumer.py").write_text(spec["consumer"])
    return pkg, tests


# ---------------------------------------------------------------- (a)


def test_real_tree_schema_and_exit_zero():
    rep = wc.scan(PROP, REPO / "tests")
    assert set(rep) == {"knobs_orphaned", "state_files_ghost",
                        "events_dead", "scanned_files"}
    assert isinstance(rep["knobs_orphaned"], list)
    assert isinstance(rep["state_files_ghost"], list)
    assert isinstance(rep["events_dead"], list)
    assert rep["scanned_files"] >= 20
    for k in rep["knobs_orphaned"]:
        assert k["knob"].startswith("PROP_")
        assert k["field"] and k["line"] > 0
    for g in rep["state_files_ghost"]:
        assert g["classification"] in ("write_only", "read_only",
                                       "declared_only")
    for e in rep["events_dead"]:
        assert e["event"] and e["file"] and e["line"] > 0

    proc = subprocess.run(
        [sys.executable, "-m", "prop_harness.tools.wiring_census"],
        capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert set(data) == set(rep)


def test_real_tree_cli_out(tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, "-m", "prop_harness.tools.wiring_census",
         "--out", str(out)],
        capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0
    assert json.loads(out.read_text())["scanned_files"] >= 20


# ---------------------------------------------------------------- (b)


def test_synthetic_leaky_tree_flags_all_three_classes(tmp_path):
    pkg, tests = _fixture(tmp_path, "leaky")
    rep = wc.scan(pkg, tests)

    orphans = {(k["knob"], k["field"]) for k in rep["knobs_orphaned"]}
    assert ("PROP_ORPHAN_KNOB", "orphan_knob") in orphans
    assert ("PROP_WIRED_KNOB", "wired_knob") not in orphans

    ghosts = {g["file"]: g for g in rep["state_files_ghost"]}
    assert ghosts["ghost.json"]["classification"] == "write_only"
    assert "wired.json" not in ghosts

    dead = {e["event"] for e in rep["events_dead"]}
    assert "dead_event" in dead
    assert "used_event" not in dead


# ---------------------------------------------------------------- (c)


def test_synthetic_wired_clean_tree_reports_empty(tmp_path):
    pkg, tests = _fixture(tmp_path, "clean")
    rep = wc.scan(pkg, tests)
    assert rep["knobs_orphaned"] == []
    assert rep["state_files_ghost"] == []
    assert rep["events_dead"] == []
    assert rep["scanned_files"] == 3


# ---------------------------------------------------------------- (d)


def test_real_tree_basket_tp_verdict():
    """Pin the actual scan verdict for basket_tp.json (2026-11-17):
    the literal is ABSENT from the tree — writer removed, no reader,
    no surviving literal — so the census cannot and does not flag it.
    If the literal ever returns wired on only one side, the census
    must flag it; re-encode this pin then."""
    src_hits = [
        p for p in PROP.rglob("*.py")
        if "__pycache__" not in p.parts and "basket_tp.json" in p.read_text()
    ]
    assert src_hits == [], (
        "basket_tp.json literal reappeared in-tree: {src_hits} — "
        "check the census ghost verdict and re-encode this pin")

    rep = wc.scan(PROP, REPO / "tests")
    ghost_files = {g["file"] for g in rep["state_files_ghost"]}
    assert "basket_tp.json" not in ghost_files
