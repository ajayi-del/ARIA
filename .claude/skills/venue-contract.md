---
name: venue-contract
description: Venue-boundary forensics — Aster/SoDEX client contract breaks (kwargs, return types, order semantics); docs + go-sdk + live probes are the spec
allowed-tools: [Bash, Read, Grep, WebFetch]
when_to_use: Before touching execution/*_client.py or venue data planes; on order rejections with exchange error strings; when wiring a new venue path
arguments:
  - name: target
    type: string
    description: "Venue method or rejection being investigated"
---

# Venue Contract Skill

Every venue-boundary bug in this fund's history was the same shape: the
code's assumption about the exchange's contract diverged from the real one.
2026-08-17 triple break (qty= vs size=, bool vs OrderResult, reduceOnly+
closePosition rejected), 08-29 membership-only leverage cache, ZEC
place_bracket ignoring candidate.size. Lesson codified: contract tests need
BOTH axes — kwarg names AND return types.

## 1. Docs are the spec — consult BEFORE writing (operator directive 2026-09-02)
- Aster: live V3 base `https://fapi.asterdex.com` (docs' fapi3 host is
  STALE). V3 auth = nonce(µs) + signer + EIP-712. V1 HMAC dead for new keys.
- SoDEX: gateway `https://mainnet-gw.sodex.dev`; the wire/signature spec is
  the GitHub repo `sodex-tech/sodex-go-sdk-public` — Go struct field order
  is the canonical marshaling (the cancel-order bug: orderID-first JSON
  recovered a garbage signer address; NO cancel had ever succeeded).
- Fetch the relevant doc section (WebFetch) before changing any client
  method. Exchange error strings are part of the spec — log them verbatim.

## 2. The break taxonomy (check all four when a venue path misbehaves)
- KWARG AXIS: boundary passes size=/new_stop_price=, client took qty=/price=
  — the call "works" (no exception) and does nothing.
- RETURN AXIS: boundary reads .success, client returned bool — silence
  downstream (startup_stop_exception after every restart class).
- ORDER SEMANTICS: reduceOnly+closePosition rejected "not required";
  closePosition=true on MARKET rejected (Aster V3) → close via place_order
  with exchange-reported qty; 503 = status UNKNOWN → reconcile, NEVER
  blind-retry.
- STATE AXIS: leverage cache membership-only (short-circuit forever after
  first set) — cache maps symbol→CONFIRMED value; short-circuit only when
  target == cached; fallback chain records the ACTUAL leverage.

## 3. Size doctrine at the boundary
candidate.size = INTENT, equity-derived size = CEILING — the venue client
takes min(intent, ceiling), floors to step (rounding can never exceed the
cap), fails closed (`aster_candidate_size_missing`) when intent is absent.
Kant/Nietzsche/WillEngine multipliers are upstream; the client never
re-derives size from equity alone (the ZEC 10× kill shot).

## 4. Prove it against the live exchange, not the docs
The docs said fapi3 (stale); the docs' cancel JSON shape was wrong for
SoDEX. Final arbiter is a live probe with the real client class on the
server (scp a script, never heredoc): place/replace/cancel on a dust-sized
order, read the exchange's own error strings, then write the contract test
pinning what the EXCHANGE does. Record new divergences in the deploy's
CLAUDE.md entry — the errata list is how the next session inherits them.

## Canon lens (baked into the steps — Dayo's working books)
- **Hasbrouck** (step 1/4): the exchange's behavior is the only market
  microstructure truth; docs are a prior, probes are the measurement.
- **Taleb** (step 2): every venue failure mode fails CLOSED — stop-placement
  failure retries then closes at market and stands down; never improvises.
- **Aronson** (step 3): the size contract is a bounded degree of freedom —
  intent vs ceiling with a hard floor-to-step, no unbounded re-derivation.
- **Simon** (step 4): write the divergence down where the next amnesiac
  process will read it — errata in CLAUDE.md, not in chat memory.
