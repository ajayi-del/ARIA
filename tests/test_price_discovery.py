"""Pins for Hasbrouck information share."""
import random
import unittest

from intelligence.price_discovery import hasbrouck_information_share


def _pair(n=600, lead_a=1.0, seed=42, noise=0.00015):
    """Venue A sees the efficient price now; venue B's quote mixes the current
    efficient price at weight lead_a with the PREVIOUS one at (1−lead_a) —
    lead_a=0.0 means B quotes last period's price (fully stale), lead_a=1.0
    means B is just as current as A (share should split ~0.5).

    Observation noise must sit well below efficient-price vol (0.0015) —
    noise ≈ vol injects an MA(1) microstructure component that pollutes the
    VAR and drags the leader's share toward 0.5."""
    random.seed(seed)
    m, a, b = [100.0], [100.0], [100.0]
    eff = 100.0
    prev_eff = 100.0
    for _ in range(n):
        prev_eff = eff
        eff *= 1 + random.gauss(0, 0.0015)
        a.append(eff * (1 + random.gauss(0, noise)))
        b_obs = (lead_a * eff + (1 - lead_a) * prev_eff)
        b.append(b_obs * (1 + random.gauss(0, noise)))
        m.append(eff)
    return a[1:], b[1:]


class TestInformationShare(unittest.TestCase):
    def test_leader_gets_high_share(self):
        a, b = _pair(lead_a=0.0)          # B fully stale → A should dominate
        r = hasbrouck_information_share(a, b, lags=8)
        self.assertIsNotNone(r)
        self.assertGreater(r["is_a_mid"], 0.6)

    def test_symmetric_pair_split_share(self):
        random.seed(9)
        eff, a, b = 100.0, [], []
        for _ in range(600):
            eff *= 1 + random.gauss(0, 0.0015)
            a.append(eff * (1 + random.gauss(0, 0.00015)))
            b.append(eff * (1 + random.gauss(0, 0.00015)))
        r = hasbrouck_information_share(a, b, lags=8)
        self.assertIsNotNone(r)
        self.assertGreater(r["is_a_mid"], 0.2)
        self.assertLess(r["is_a_mid"], 0.8)

    def test_bounds_order(self):
        a, b = _pair()
        r = hasbrouck_information_share(a, b, lags=8)
        self.assertLessEqual(r["is_a_lo"], r["is_a_mid"])
        self.assertLessEqual(r["is_a_mid"], r["is_a_hi"])

    def test_thin_and_bad_data_none(self):
        self.assertIsNone(hasbrouck_information_share([1, 2, 3], [1, 2, 3]))
        self.assertIsNone(hasbrouck_information_share([0.0] * 300, [1.0] * 300))
        self.assertIsNone(hasbrouck_information_share([1.0] * 300, [1.0] * 299))

    def test_constant_series_none_or_stable(self):
        r = hasbrouck_information_share([100.0] * 300, [100.0] * 300)
        self.assertTrue(r is None or 0.0 <= r["is_a_mid"] <= 1.0)


if __name__ == "__main__":
    unittest.main()
