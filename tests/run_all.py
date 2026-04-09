#!/usr/bin/env python3
"""
ARIA Complete Test Suite
Run: python tests/run_all.py
"""

import unittest
import sys
import time

def run_phase(name, module):
    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(module)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()

if __name__ == "__main__":
    # Ensure current dir is in path for imports
    import os
    sys.path.append(os.getcwd())

    import tests.test_phase1 as p1
    import tests.test_phase2 as p2
    import tests.test_phase3 as p3
    import tests.test_phase4 as p4
    import tests.test_phase45 as p45
    import tests.test_phase7 as p7
    import tests.test_assets as pa
    import tests.test_calendar as tc
    import tests.test_phase10 as p10

    start = time.time()
    results = {
        "Phase 1 — Data Layer": run_phase("PHASE 1 — DATA LAYER", p1),
        "Phase 2 — Intelligence": run_phase("PHASE 2 — INTELLIGENCE", p2),
        "Phase 3 — Execution": run_phase("PHASE 3 — EXECUTION", p3),
        "Phase 4 — Memory": run_phase("PHASE 4 — MEMORY", p4),
        "Phase 4.5 — Funding Radar": run_phase("PHASE 4.5 — FUNDING", p45),
        "Phase 7 — Quant Layer": run_phase("PHASE 7 — QUANT", p7),
        "Asset Expansion — 7 Assets": run_phase("ASSETS — 7 COINS", pa),
        "Phase 9 — Calendar Engine": run_phase("PHASE 9 — CALENDAR", tc),
        "Phase 10 — Event-Driven": run_phase("PHASE 10 — EVENT-DRIVEN", p10),
    }

    elapsed = time.time() - start

    print(f"\n{'='*50}")
    print(f"  ARIA TEST SUITE RESULTS")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"{'='*50}")

    all_passed = True
    for phase, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  {phase}")
        if not passed:
            all_passed = False

    print(f"{'='*50}")
    if all_passed:
        print("  ALL PHASES PASSING")
        print("  ARIA is ready for testnet")
    else:
        print("  FAILURES DETECTED")
        print("  Fix before switching to testnet")
    print(f"{'='*50}\n")

    sys.exit(0 if all_passed else 1)
