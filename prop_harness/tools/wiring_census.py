"""wiring_census — standing audit for writer/reader drift in prop_harness.

Three defect classes, all statically scanned (stdlib ast only):

1. ORPHAN KNOBS — every ``PROP_*`` env knob parsed in
   ``prop_harness/config.py``'s ``PropConfig.from_env`` whose resulting
   dataclass field has zero attribute reads anywhere else in
   ``prop_harness/`` (``config.<field>``, ``getattr(config, "<field>")``,
   or a ``self.<field>`` property read inside config.py itself).
   The 2026-11-14 surplus-routing incident class: a knob parsed but
   never read is a knob that cannot act.

2. GHOST STATE FILES — ``*.json`` / ``*.jsonl`` path literals in the
   tree classified write_only (written, never read in-tree),
   read_only (read, never written in-tree) or declared_only (literal
   exists, zero read/write call sites resolve to it — the
   removed-writer class; a literal fully ABSENT from the tree is not
   reportable by any static scan and is therefore out of scope).
   Writes/reads are attributed through: direct literal call args,
   file-local aliases (``self.x = root / "f.json"``), module-level
   constants and property functions (multi-hop, e.g.
   ``config.kingdom_view_artifact`` → ``DEFAULT_VIEW_PATH`` →
   ``prop_kingdom_view.json``), and constructor injection
   (``BudgetBrain(root / "budget_day.json")`` inherits the class's
   read_json/atomic_write_json capability).
   Write helpers: write_text, atomic_write_json, append_event, open(w),
   json.dump, write_*/save_*/publish_* prefixes.
   Read helpers: read_text, read_json, open(r), json.load,
   read_*/load_*/fetch_* prefixes.

3. DEAD EVENTS — event names emitted via the tree's two idioms
   (structlog-style ``log.info("name", ...)`` and journal
   ``append_event(journal, {"type": "name", ...})``) that appear in no
   test file and as no other string constant anywhere in
   ``prop_harness/`` (no consumer). Telemetry humans grep for will
   flag here by design — the list is the audit surface.

CLI: ``python -m prop_harness.tools.wiring_census [--root DIR]
[--tests-root DIR] [--out PATH]``. Always exits 0 (best-effort
doctrine — an audit tool must never red a build).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

STATE_RE = re.compile(r"^[~./\w@-]*[\w@-]+\.jsonl?$")
GETATTR_RE_TMPL = r"getattr\(\s*[\w.\[\]()]+,\s*['\"]%s['\"]"

_LOG_METHODS = {"info", "warning", "error", "debug", "critical", "exception"}
_EVENT_DICT_FUNCS = {"append_event", "log_event", "emit_event"}

_WRITE_FUNCS = {"atomic_write_json", "write_json", "append_event", "dump"}
_READ_FUNCS = {"read_json", "load_json", "load"}
_WRITE_PREFIXES = ("atomic_write", "write_", "save_", "publish_")
_READ_PREFIXES = ("read_", "load_", "fetch_")


def _py_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts
    )


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- knobs


def _knobs_in_expr(node: ast.AST) -> list[tuple[str, int]]:
    """PROP_* knob references inside an expression (f/i/b helper calls,
    os.environ.get literals, ``"PROP_X" in os.environ`` comparisons)."""
    out: list[tuple[str, int]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if (isinstance(f, ast.Name) and f.id in ("f", "i", "b")
                    and sub.args and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                out.append(("PROP_" + sub.args[0].value, sub.lineno))
            elif (isinstance(f, ast.Attribute) and f.attr == "get"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "environ"
                    and sub.args and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                    and sub.args[0].value.startswith("PROP_")):
                out.append((sub.args[0].value, sub.lineno))
        elif isinstance(sub, ast.Compare):
            for c in ast.walk(sub):
                if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and c.value.startswith("PROP_")):
                    out.append((c.value, sub.lineno))
    return out


def extract_knobs(config_path: Path) -> list[dict]:
    """Map each PROP_* knob parsed in from_env to its dataclass field."""
    tree = ast.parse(config_path.read_text(), filename=str(config_path))
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "from_env"):
            continue
        # locals whose value mentions knobs (e.g. state_root)
        var_knobs: dict[str, list[tuple[str, int]]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                ks = _knobs_in_expr(sub.value)
                if ks:
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            var_knobs.setdefault(t.id, []).extend(ks)
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "cls"):
                continue
            for kw in sub.keywords:
                if not kw.arg:
                    continue
                ks = _knobs_in_expr(kw.value)
                for n in ast.walk(kw.value):
                    if isinstance(n, ast.Name) and n.id in var_knobs:
                        ks = ks + var_knobs[n.id]
                best: dict[str, int] = {}
                for name, line in ks:
                    best[name] = min(line, best.get(name, line))
                for name, line in best.items():
                    if (name, kw.arg) not in seen:
                        seen.add((name, kw.arg))
                        rows.append({"knob": name, "field": kw.arg,
                                     "line": line})
    return rows


def field_read_counts(root: Path, fields: list[str],
                      src_files: list[Path]) -> dict[str, int]:
    counts = {f: 0 for f in fields}
    if not fields:
        return counts
    pats = {f: re.compile(r"\.%s\b" % re.escape(f)) for f in fields}
    gats = {f: re.compile(GETATTR_RE_TMPL % re.escape(f)) for f in fields}
    for p in src_files:
        try:
            text = p.read_text()
        except OSError:
            continue
        for f in fields:
            counts[f] += len(pats[f].findall(text))
            counts[f] += len(gats[f].findall(text))
    return counts


# ---------------------------------------------------------- state files


class _Caps:
    __slots__ = ("w", "r")

    def __init__(self) -> None:
        self.w = False
        self.r = False


def _call_kind(node: ast.Call) -> str | None:
    f = node.func
    last = ""
    if isinstance(f, ast.Attribute):
        last = f.attr
    elif isinstance(f, ast.Name):
        last = f.id
    if isinstance(f, ast.Name) and f.id == "open":
        mode = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if isinstance(mode, str):
            return "w" if any(c in mode for c in "wax+") else "r"
        return "r"
    if last == "write_text":
        return "w"
    if last == "read_text":
        return "r"
    if last in _WRITE_FUNCS:
        return "w"
    if last in _READ_FUNCS:
        return "r"
    for pre in _WRITE_PREFIXES:
        if last.startswith(pre):
            return "w"
    for pre in _READ_PREFIXES:
        if last.startswith(pre):
            return "r"
    return None


def _collect_literal_refs(node: ast.AST) -> tuple[set[str], set[str]]:
    """(state-file literals, name references) inside an expression."""
    lits: set[str] = set()
    refs: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            v = sub.value
            if STATE_RE.match(v) and " " not in v and "://" not in v:
                lits.add(v)
        elif isinstance(sub, ast.Name):
            refs.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            refs.add(sub.attr)
    return lits, refs


def _build_aliases(tree: ast.AST) -> tuple[dict, dict]:
    """Per-file alias graph. Returns (module_level, file_local) maps of
    name -> {"lits": set, "refs": set}. Function bodies (incl. @property)
    contribute module-level entries keyed by function name; Assign
    targets contribute module-level for bare Names at module depth,
    file-local otherwise (attributes, function locals)."""
    module_level: dict[str, dict] = {}
    file_local: dict[str, dict] = {}

    def add(m: dict, name: str, lits: set[str], refs: set[str]) -> None:
        e = m.setdefault(name, {"lits": set(), "refs": set()})
        e["lits"] |= lits
        e["refs"] |= refs

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            lits, refs = _collect_literal_refs(node)
            add(module_level, node.name, lits, refs)
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node: ast.Assign) -> None:
            lits, refs = _collect_literal_refs(node.value)
            if lits or refs:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        m = module_level if self.depth == 0 else file_local
                        add(m, t.id, lits, refs)
                    elif isinstance(t, ast.Attribute):
                        add(file_local, t.attr, lits, refs)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                lits, refs = _collect_literal_refs(node.value)
                if lits or refs:
                    t = node.target
                    if isinstance(t, ast.Name):
                        m = module_level if self.depth == 0 else file_local
                        add(m, t.id, lits, refs)
                    elif isinstance(t, ast.Attribute):
                        add(file_local, t.attr, lits, refs)
            self.generic_visit(node)

    V().visit(tree)
    return module_level, file_local


def _class_caps(tree: ast.AST) -> dict[str, _Caps]:
    caps: dict[str, _Caps] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            c = caps.setdefault(node.name, _Caps())
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    k = _call_kind(sub)
                    if k == "w":
                        c.w = True
                    elif k == "r":
                        c.r = True
    return caps


def _resolve(name: str, local: dict, gmod: dict,
             seen: set[str] | None = None) -> set[str]:
    if seen is None:
        seen = set()
    if name in seen:
        return set()
    seen.add(name)
    out: set[str] = set()
    for m in (local, gmod):
        e = m.get(name)
        if not e:
            continue
        out |= e["lits"]
        for r in e["refs"]:
            out |= _resolve(r, local, gmod, seen)
    return out


def scan_state_files(root: Path, src_files: list[Path],
                     test_files: list[Path]) -> list[dict]:
    per_file: dict[Path, tuple[dict, dict, dict[str, _Caps]]] = {}
    all_lits: set[str] = set()
    global_mod: dict[str, dict] = {}
    global_caps: dict[str, _Caps] = {}
    for p in src_files:
        try:
            tree = ast.parse(p.read_text(), filename=str(p))
        except (OSError, SyntaxError):
            continue
        mod, loc = _build_aliases(tree)
        caps = _class_caps(tree)
        per_file[p] = (mod, loc, caps)
        for name, e in mod.items():
            g = global_mod.setdefault(name, {"lits": set(), "refs": set()})
            g["lits"] |= e["lits"]
            g["refs"] |= e["refs"]
        for name, c in caps.items():
            g2 = global_caps.setdefault(name, _Caps())
            g2.w = g2.w or c.w
            g2.r = g2.r or c.r
        for e in list(mod.values()) + list(loc.values()):
            all_lits |= e["lits"]

    writes: dict[str, set[str]] = {}
    reads: dict[str, set[str]] = {}
    for p, (mod, loc, caps) in per_file.items():
        try:
            tree = ast.parse(p.read_text(), filename=str(p))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _call_kind(node)
            f = node.func
            last = (f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
            ctor = global_caps.get(last)
            if kind is None and ctor is not None:
                kind = "w" if ctor.w else ("r" if ctor.r else None)
                # a class that both reads and writes its injected path
                if ctor.w and ctor.r:
                    kind = "wr"
            if kind is None:
                continue
            lits: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    v = sub.value
                    if STATE_RE.match(v) and " " not in v and "://" not in v:
                        lits.add(v)
                elif isinstance(sub, ast.Name):
                    lits |= _resolve(sub.id, loc, global_mod)
                elif isinstance(sub, ast.Attribute):
                    lits |= _resolve(sub.attr, loc, global_mod)
            site = f"{_rel(root, p)}:{node.lineno}"
            for lit in lits:
                all_lits.add(lit)
                if "w" in kind:
                    writes.setdefault(lit, set()).add(site)
                if "r" in kind:
                    reads.setdefault(lit, set()).add(site)

    test_text = ""
    for p in test_files:
        try:
            test_text += p.read_text()
        except OSError:
            continue

    ghosts: list[dict] = []
    for lit in sorted(all_lits):
        w = sorted(writes.get(lit, set()))
        r = sorted(reads.get(lit, set()))
        if w and r:
            continue
        cls = ("write_only" if w else "read_only" if r
               else "declared_only")
        base = lit.split("/")[-1]
        ghosts.append({
            "file": lit,
            "classification": cls,
            "written_in": w,
            "read_in": r,
            "referenced_in_tests": base in test_text,
        })
    ghosts.sort(key=lambda g: (g["classification"], g["file"]))
    return ghosts


# -------------------------------------------------------------- events


def scan_events(root: Path, src_files: list[Path],
                test_files: list[Path]) -> list[dict]:
    emitted: dict[str, list[str]] = {}
    # per-file: total string-constant occurrences minus emission-site
    # occurrences = consumer references (single parse — node identity
    # does not survive a re-parse, so count by position in one walk)
    src_ref_counts: dict[str, int] = {}
    for p in src_files:
        try:
            tree = ast.parse(p.read_text(), filename=str(p))
        except (OSError, SyntaxError):
            continue
        file_consts: dict[str, int] = {}
        file_emits: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                file_consts[node.value] = file_consts.get(node.value, 0) + 1
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            last = (f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
            name = None
            if (isinstance(f, ast.Attribute) and f.attr in _LOG_METHODS
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                name = node.args[0].value
            elif last in _EVENT_DICT_FUNCS:
                for a in list(node.args[1:]) + [kw.value for kw in
                                                node.keywords]:
                    if isinstance(a, ast.Dict):
                        for k, v in zip(a.keys, a.values):
                            if (isinstance(k, ast.Constant)
                                    and k.value == "type"
                                    and isinstance(v, ast.Constant)
                                    and isinstance(v.value, str)):
                                name = v.value
            if name:
                emitted.setdefault(name, []).append(
                    f"{_rel(root, p)}:{node.lineno}")
                file_emits[name] = file_emits.get(name, 0) + 1
                src_ref_counts.setdefault(name, 0)
        for name, total in file_consts.items():
            if name in src_ref_counts:
                src_ref_counts[name] += total - file_emits.get(name, 0)

    test_text = ""
    for p in test_files:
        try:
            test_text += p.read_text()
        except OSError:
            continue

    dead: list[dict] = []
    for name, sites in sorted(emitted.items()):
        in_tests = (f'"{name}"' in test_text) or (f"'{name}'" in test_text)
        if src_ref_counts[name] == 0 and not in_tests:
            first = sites[0]
            fpath, _, line = first.rpartition(":")
            dead.append({"event": name, "file": fpath,
                         "line": int(line), "sites": sites})
    return dead


# --------------------------------------------------------------- driver


def scan(root: Path, tests_root: Path | None = None) -> dict:
    root = Path(root)
    src_files = _py_files(root)
    test_files: list[Path] = []
    if tests_root is not None and Path(tests_root).is_dir():
        test_files = _py_files(Path(tests_root))

    knobs = extract_knobs(root / "config.py")
    fields = sorted({k["field"] for k in knobs})
    counts = field_read_counts(root, fields, src_files)
    knobs_orphaned = sorted(
        (k for k in knobs if counts.get(k["field"], 0) == 0),
        key=lambda k: k["knob"])

    return {
        "knobs_orphaned": knobs_orphaned,
        "state_files_ghost": scan_state_files(root, src_files, test_files),
        "events_dead": scan_events(root, src_files, test_files),
        "scanned_files": len(src_files),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_root = Path(__file__).resolve().parents[1]
    ap.add_argument("--root", default=str(default_root),
                    help="prop_harness source root")
    ap.add_argument("--tests-root",
                    default=str(default_root.parent / "tests"),
                    help="tests tree scanned for event/file consumers")
    ap.add_argument("--out", default="", help="write report here "
                    "instead of stdout")
    args = ap.parse_args(argv)
    try:
        report = scan(Path(args.root),
                      Path(args.tests_root) if args.tests_root else None)
    except Exception as exc:  # best-effort doctrine: never red a build
        report = {"error": f"{type(exc).__name__}: {exc}",
                  "knobs_orphaned": [], "state_files_ghost": [],
                  "events_dead": [], "scanned_files": 0}
    blob = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        try:
            Path(args.out).write_text(blob + "\n")
        except OSError as exc:
            print(f"wiring_census: --out write failed: {exc}",
                  file=sys.stderr)
            print(blob)
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
