# Department Template — how ARIA grows without breaking

Set 2026-08-19. Doctrine from Herbert Simon (*The Sciences of the Artificial* —
nearly decomposable systems), W. Ross Ashby (requisite variety), and Stafford
Beer (Viable System Model). Proven in production by the Treasury subsystem.

A "department" is any new autonomous subsystem (Treasury, future ALM desk,
research engine, router brain, new execution sleeve). Every department MUST
follow this template. If a proposed module cannot, it is not ready to build.

## 1. The shape (Simon)

The system grows by accreting stable, nearly-decomposable modules — never by
entangling `main.py` further. A department is defined by its interface; its
internals are free to change.

1. **Pure-logic brain module** in `intelligence/` or `execution/`.
   - Zero I/O: no network, no file access, no exchange calls, no direct logging
     (return telemetry in its decision objects instead).
   - All external data injected as callables (`mark_fn`, `venue_fn`, ...) or
     plain arguments. All tunables read from an injected config object.
   - Owns its own state; imports nothing from `main.py`.
   - Exemplar: `intelligence/treasury.py`.

2. **Exactly ONE splice point in `main.py`** — a thin executor that gathers
   inputs, calls the brain, and performs the I/O for its decisions. The brain
   decides; the executor acts. (Exemplar: the treasury executor loop.)

3. **Kill switch whose `False` state reproduces the pre-module system exactly.**
   - Config knob `<dept>_enabled: bool`.
   - Disabled behavior must be indistinguishable from the module not existing —
     including handing any owned resources back (precedent: treasury ownership
     release on deactivate).

4. **Own telemetry namespace + own test file.**
   - Log events prefixed `<dept>_`.
   - `tests/test_<dept>.py` — the brain fully testable without booting the bot.

## 2. Variety budget (Ashby)

Only variety absorbs variety. Every new source of *behavioral* variety MUST
ship with matching *regulatory* variety, in the same commit:

- a kill switch (above),
- telemetry counters sufficient to distinguish its failure modes,
- digest / EV-scan coverage (or an explicit note why existing sections cover it),
- a "designed events" entry in CLAUDE.md so the watchdog does not "fix" its
  normal behavior.

**No unobserved degrees of freedom.** If you cannot enumerate how the module
fails, do not ship it.

## 3. VSM home (Beer)

Every department declares exactly ONE Viable System Model function:

| Function | Role | Current occupants |
|---|---|---|
| S1 operations | execution sleeves | SoDEX book, Aster sleeve (future: AUGUR) |
| S2 coordination | anti-oscillation between S1 units | router, param store, cooldown registries |
| S3 control | inside-and-now resource allocation | Treasury |
| S3* audit | inspection bypassing S1 self-report | shadow journal, watchdog |
| S4 intelligence | outside-and-then adaptation | EV scan, digest, venue comparison |
| S5 policy | identity / ultimate authority | Chancellor + CLAUDE.md hard rules |

Algedonic channel (pain signal bypassing the hierarchy): Chancellor veto and
the watchdog's bounded fix authority.

One function per department. If a module does two, split it. (The pre-Treasury
basket deadlock was a broken S2/S3 boundary — five controllers on one stock.)
