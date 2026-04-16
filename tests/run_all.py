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
    import tests.test_institutional as ti
    import tests.test_sovereign as ts
    import tests.test_philosophy as tph

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
        "ATR Gate Fix": run_phase("ATR GATE", ti.TestATRGateRemoved),
        "Six Personalities": run_phase("PERSONALITIES", ti.TestSixPersonalities),
        "Hysteresis": run_phase("HYSTERESIS", ti.TestPersonalityHysteresis),
        "Context Cache": run_phase("CONTEXT CACHE", ti.TestPersonalityContextCache),
        "Budget Manager": run_phase("BUDGET", ti.TestBudgetManager),
        "Prediction Market": run_phase("PREDICTION MKT", ti.TestPredictionMarket),
        "Terrain Coherence": run_phase("TERRAIN", ti.TestTerrainCoherence),
        "Agent Terrain Rules": run_phase("AGENT RULES", ti.TestAgentTerrainRules),
        "RPC and Freeze": run_phase("RPC FREEZE", ti.TestRPCAndFreeze),
        "ML Classifier": run_phase("ML", ti.TestMLClassifier),
        "Latency Budget": run_phase("LATENCY", ti.TestLatencyBudget),
        "Integration": run_phase("INTEGRATION", ti.TestIntegration),
        # Phase 14 — SOVEREIGN Personality
        "SOVEREIGN — Component Monitor":  run_phase("SOVEREIGN MONITOR",  ts.TestSSIComponentMonitor),
        "SOVEREIGN — Staking Monitor":    run_phase("SOVEREIGN STAKING",  ts.TestStakingMonitor),
        "SOVEREIGN — Yield Tracker":      run_phase("SOVEREIGN YIELD",    ts.TestYieldTracker),
        "SOVEREIGN — Signal Generator":   run_phase("SOVEREIGN SIGNAL",   ts.TestSovereignSignalGenerator),
        "SOVEREIGN — Personality Routing":run_phase("SOVEREIGN ROUTING",  ts.TestSovereignPersonalityRouting),
        "SOVEREIGN — Budget Integration": run_phase("SOVEREIGN BUDGET",   ts.TestSovereignBudgetIntegration),
        "SOVEREIGN — Cross-Agent":        run_phase("SOVEREIGN AGENTS",   ts.TestSovereignCrossAgentIndependence),
        "SOVEREIGN — Latency":            run_phase("SOVEREIGN LATENCY",  ts.TestSovereignLatency),
        # Phase 15 — Philosophical Agency
        "Philosophy — P1  Territory vs Budget":    run_phase("PHIL P1",  tph.TestTerritoryIsNotBudget),
        "Philosophy — P2  Campaigns from Income":  run_phase("PHIL P2",  tph.TestCampaignsFundedByIncome),
        "Philosophy — P4  Discrete Modes":         run_phase("PHIL P4",  tph.TestPersonalitiesAreDiscreteStates),
        "Philosophy — P5  SHIELD Absolute":        run_phase("PHIL P5",  tph.TestShieldIsAbsolute),
        "Philosophy — P6  Orthogonal Edges":       run_phase("PHIL P6",  tph.TestEdgesAreOrthogonal),
        "Philosophy — P7  Flow Gradient":          run_phase("PHIL P7",  tph.TestFlowFollowsGradient),
        "Philosophy — P8  APEX Momentum":          run_phase("PHIL P8",  tph.TestApexRequiresMaxMomentum),
        "Philosophy — P9  SCOUT Fallback":         run_phase("PHIL P9",  tph.TestScoutIsAlwaysFallback),
        "Philosophy — P10 COIL Siege":             run_phase("PHIL P10", tph.TestCoilIsSiegePatience),
        "Philosophy — P12 Kelly Discipline":       run_phase("PHIL P12", tph.TestKingdomNeverOverExtends),
        "Philosophy — P14 Structural Matching":    run_phase("PHIL P14", tph.TestStructuralMatching),
        "Philosophy — P15 Cycle Renewal":          run_phase("PHIL P15", tph.TestTheCycleRenews),
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
        print("  ARIA is philosophically and technically ready for mainnet")
    else:
        print("  FAILURES DETECTED")
        print("  Fix before going live on mainnet")
    print(f"{'='*50}\n")

    sys.exit(0 if all_passed else 1)
