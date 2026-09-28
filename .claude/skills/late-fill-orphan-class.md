# Late-fill orphan class (ARIA)

**Symptom:** exchange shows a position with `--/--` TP/SL, engine heartbeats normally, no stop/hedge/treasury ever touches it; rides deep red.

**Provenance chain (verified 2026-09-28):**
1. Anticipator evicts a resting order → journals its intent `outcome="rejected"` (R6 doctrine) and pops the fleet row (OrderSpec with stop_price/tp_price destroyed).
2. Venue cancel races a fill → `cancel_hole_late_fill` ("cancelled entry filled late — reconciliation adopts").
3. Adoption registers SIZE only — no geometry, no ownership, no journal intent.
4. Every protection layer structurally skips it: software guardian (stop=None), fork/budget hedge (`no_stop` fail-closed), campaign hedge (not a family member), treasury (not a ledger member). Operator firewall may also misclassify it as a manual position (no live intent → "operator's, never manage").

**Detection greps (server):**
```
grep "cancel_hole_late_fill" logs/aria.log | tail          # the adoption events
grep "anticipator_fill_attributed" logs/aria.log           # fills WITH ownership — orphans are the late-fills NOT in this list
grep <SYM> logs/aria.log | grep -iE "stop|hedge" | tail    # protection silence = orphan
```
Orphan test: late-fill present + fill_attributed absent + zero stop/hedge events for the symbol after adoption.

**Fix doctrine:**
- L1 at the adoption site: register ownership + immediate protective stop (max(2%, 1.0×ATR15/mark) from fill mark) + journal intent with provenance=anticipator_late_fill.
- L2: synthesize stop geometry for any stop-less adopted position so fork RED pain-harvest can arm.
- Root: keep the eviction conveyor dead (`anticipator_inplace_upgrade_enabled` False) — every eviction is a cancel-race lottery ticket.

**Rule:** a cancelled order's fill is still ARIA's child. Never adopt size without geometry.
