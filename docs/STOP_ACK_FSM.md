# Prop Stop-Ack FSM

**Cite:** management-layer §6 build #1 · ATS-22 · Graph (6)  
**MUST-NOT:** no Kant / Nietzsche / Chancellor / tier rewrite; brain writes no stops.

## Law

| State | Meaning |
|-------|---------|
| `ARMED` | Protective stop placed; awaiting venue confirmation |
| `ACKED` | Resting `stop_market` + `reduceOnly` observed on exit side |
| `MISSED` | Ack timeout, lost confirmation, or dark order plane after ACKED |

```
place / resize heal → ARMED → (open orders present) → ACKED
                         ↘ timeout / lost / unknown@ACKED → MISSED
MISSED → heal (resize_protection) → ARMED on success
       → else reduce (cancel + close) when PROP_STOP_ACK_REDUCE=1
any open MISSED → HALT new place_bracket / reload / tap
```

**Fail-closed:** `ACKED` + absent **or** unknown open-orders → `MISSED`. Missing protection after ack is never treated as still protected.

## Kill switches

| Env | Default | Off behavior |
|-----|---------|--------------|
| `PROP_STOP_ACK_FSM` | on | Legacy bit-for-bit (no stamps, no halt, no heal/reduce) |
| `PROP_STOP_ACK_REDUCE` | on | Halt + heal only — no close of naked exposure |
| `PROP_STOP_ACK_TIMEOUT_S` | `30` | Seconds ARMED may await before MISSED |

## Surfaces

- Pure brain: `prop_harness/brains/stop_ack.py`
- Actuation: `ProprExecutor.reconcile_stop_ack` + place/reload halt
- Loop: `PropHarness.reconcile` (after size/BE/trail); `tap_stop_ack_halt`
- Telemetry: `stop_ack_missed`, `stop_ack_acked`, `stop_ack_armed`, `stop_ack_heal`, `stop_ack_reduced`

Trail / BE / resize ownership unchanged — FSM observes and may *call* existing `resize_protection`; it does not invent a second stop writer.
