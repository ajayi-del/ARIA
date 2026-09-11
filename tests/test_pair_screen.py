"""Pure-math pins for tools/pair_screen.py — no network."""

import importlib.util
import json
import math
import os
import random

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "pair_screen.py")
_spec = importlib.util.spec_from_file_location("pair_screen", _PATH)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def _rw(rng, n, sigma=0.01):
    out, v = [], 0.0
    for _ in range(n):
        v += rng.gauss(0, sigma)
        out.append(v)
    return out


def _ar1(rng, n, phi, sigma=0.01):
    out, v = [], 0.0
    for _ in range(n):
        v = phi * v + rng.gauss(0, sigma)
        out.append(v)
    return out


def _cointegrated(seed=7, n=300, b=1.8, phi=0.70):
    """Shared random walk + stationary AR(1) spread → cointegrated pair."""
    rng = random.Random(seed)
    lx = _rw(rng, n)
    spread = _ar1(rng, n, phi)
    ly = [0.3 + b * x + s for x, s in zip(lx, spread)]
    return [math.exp(v) for v in lx], [math.exp(v) for v in ly], b, phi


def _independent_walks(seed=11, n=300):
    rng = random.Random(seed)
    return ([math.exp(v) for v in _rw(rng, n)],
            [math.exp(v) for v in _rw(rng, n)])


# ── OLS ──────────────────────────────────────────────────────────────────────

def test_ols_recovers_hedge_ratio():
    rng = random.Random(3)
    x = [i * 0.1 for i in range(200)]
    y = [1.5 + 2.5 * v + rng.gauss(0, 0.01) for v in x]
    a, b, resid = ps.ols(x, y)
    assert abs(b - 2.5) < 0.02
    assert abs(a - 1.5) < 0.02
    assert len(resid) == 200


def test_ols_degenerate_variance_raises():
    try:
        ps.ols([1.0] * 10, [2.0] * 10)
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── Engle-Granger / ADF ─────────────────────────────────────────────────────

def test_adf_passes_on_cointegrated_pair():
    ca, cb, _b, _phi = _cointegrated()
    stats = ps.screen_pair(ca, cb)
    assert stats is not None
    assert stats["adf_p"] < 0.05


def test_adf_fails_on_independent_walks():
    ca, cb = _independent_walks()
    stats = ps.screen_pair(ca, cb)
    assert stats is not None
    assert stats["adf_p"] > 0.05


def test_screen_pair_recovers_hedge_ratio():
    ca, cb, b, _phi = _cointegrated(b=1.8)
    stats = ps.screen_pair(cb, ca)   # y = ly (sym_a) regressed on x = lx
    assert abs(stats["hedge_ratio"] - b) < 0.15


def test_mackinnon_anchors_and_monotonicity():
    assert abs(ps.mackinnon_p(-2.86) - 0.05) < 1e-9
    assert abs(ps.mackinnon_p(-3.41) - 0.01) < 1e-9
    ps_ = [ps.mackinnon_p(t) for t in (-4.0, -3.0, -2.5, -2.0, -1.0)]
    assert all(a <= b for a, b in zip(ps_, ps_[1:]))
    assert ps.mackinnon_p(-99.0) == 0.0005


# ── OU half-life ─────────────────────────────────────────────────────────────

def test_ou_recovers_known_half_life():
    rng = random.Random(21)
    phi = 0.70
    u = _ar1(rng, 400, phi)
    theta, hl = ps.ou_fit(u)
    expected = math.log(2) / (-math.log(phi))
    assert abs(hl - expected) < 1.0
    assert theta > 0.14


def test_ou_random_walk_spread_not_tradeable():
    rng = random.Random(23)
    u = _rw(rng, 400)
    theta, hl = ps.ou_fit(u)
    assert hl >= ps.MAX_HALF_LIFE or theta <= ps.MIN_THETA


# ── tradeable gate ───────────────────────────────────────────────────────────

def test_tradeable_gate():
    assert ps.tradeable(0.01, 2.0, 0.30) is True
    assert ps.tradeable(0.10, 2.0, 0.30) is False     # p too high
    assert ps.tradeable(0.01, 6.0, 0.30) is False     # half-life too long
    assert ps.tradeable(0.01, 2.0, 0.10) is False     # theta too small


# ── kill / survival ──────────────────────────────────────────────────────────

def test_kill_on_p_threshold():
    assert ps.kill_status(0.26, 2.0, 2.0) == "dead"
    assert ps.kill_status(0.25, 2.0, 2.0) == "candidate"


def test_kill_on_half_life_doubling():
    assert ps.kill_status(0.10, 4.01, 2.0) == "dead"
    assert ps.kill_status(0.10, 3.9, 2.0) == "candidate"
    assert ps.kill_status(0.10, 100.0, None) == "candidate"  # no entry value


# ── z-score ──────────────────────────────────────────────────────────────────

def test_zscore_now_known_series():
    u = [0.0] * 19 + [2.0]
    z = ps.zscore_now(u, window=20)
    # mean 0.1, pop std ~0.4359 → z ≈ (2.0-0.1)/0.4359
    assert abs(z - 4.359) < 0.01
    assert ps.zscore_now([1.0, 1.0, 1.0]) == 0.0      # zero variance guard


# ── alignment ────────────────────────────────────────────────────────────────

def test_align_drops_excessive_missing_overlap():
    a = {d: 1.0 for d in range(250)}
    b = {d: 1.0 for d in range(250)}
    for d in range(200, 250):       # b misses 50 of a's 250 days (20%)
        b[d + 1000] = b.pop(d)
    assert ps.align_days(a, b) is None


def test_align_accepts_tight_overlap():
    a = {d: float(d) + 1 for d in range(250)}
    b = {d: float(d) + 2 for d in range(5, 255)}
    out = ps.align_days(a, b)
    assert out is not None
    xa, xb = out
    assert len(xa) == len(xb) == 245
    assert xa[0] == 6.0 and xb[0] == 7.0


def test_align_requires_min_bars():
    a = {d: 1.0 for d in range(100)}
    b = {d: 1.0 for d in range(100)}
    assert ps.align_days(a, b) is None


# ── carry screen ─────────────────────────────────────────────────────────────

def test_carry_ok_threshold():
    # 0.0001 hourly diff × 24 × 2 days = 0.0048 > 0.0016 → ok
    assert ps.carry_ok(0.0001, 2.0) is True
    # 0.00001 × 24 × 2 = 0.00048 < 0.0016 → negative carry
    assert ps.carry_ok(0.00001, 2.0) is False
    assert ps.carry_ok(0.0001, 2.0, cost=0.01) is False


def test_funding_diff_aligned_hours():
    funding = {
        "A-USD": [{"rate": 0.0003, "timestamp_ms": h * 3600000}
                  for h in range(10)],
        "B-USD": [{"rate": 0.0001, "timestamp_ms": h * 3600000}
                  for h in range(10)],
    }
    assert abs(ps.funding_diff(funding, "A-USD", "B-USD") - 0.0002) < 1e-12
    assert ps.funding_diff(funding, "A-USD", "MISSING") is None


# ── I/O doctrine ─────────────────────────────────────────────────────────────

def test_atomic_write(tmp_path):
    path = str(tmp_path / "pair_screen.json")
    ps.atomic_write_json(path, {"ts": "t", "pairs": [{"sym_a": "A"}]})
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert json.load(open(path))["pairs"][0]["sym_a"] == "A"


def test_history_one_bad_line(tmp_path):
    path = str(tmp_path / "hist.jsonl")
    with open(path, "w") as f:
        f.write('{"ts": "a", "n_candidates": 1}\n')
        f.write('{"ts": "BROKEN"\n')
        f.write('not json at all\n')
        f.write('{"ts": "b", "n_candidates": 2}\n')
    rows = ps.read_history(path)
    assert len(rows) == 2
    assert rows[0]["ts"] == "a" and rows[1]["ts"] == "b"
    assert ps.read_history(str(tmp_path / "absent.jsonl")) == []
