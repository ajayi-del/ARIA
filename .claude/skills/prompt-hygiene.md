# Prompt Hygiene — LLM-node prompts are contracts, not logs

Origin: 2026-09-06 Cato prompt v1 collapse (95,028 bytes; injected bundle 108,364 vs the
~120k argv-adjacent tripwire; ~8 cache-create payments/day). Same class as the CEO's D8
ceo_directives.md 5KB law. Applies to every LLM node prompt (Cato, CEO, any future node).

## The failure mode
Append-only accretion: dated tasks, per-deploy "DEPLOYED <sha>" sections, expired
settle-checks, "AMENDMENT" blocks. Every incident adds a section; nothing ever leaves.
Byte growth is monotonic until the bundle hits a wall (argv limit, cache-create cost,
or the model drowning in stale instruction).

## The law
1. **Prompt = constitution.** Identity, authority lanes, protocols, standing mandates.
   It does not carry deploy history, incident detail, or dated tasks.
2. **Hard cap declared in the file header** (Cato: 30KB). A bundle budget alongside it
   (Cato: 45KB for prompt + injected memory files), reported by the runner every cycle.
3. **Registers, not sections.** Per-deploy watch items, designed-events, incident notes →
   one line each in a read-on-demand register file (watch_register.md, designed_events.md).
   The prompt holds a pointer, not the content.
4. **Edit in place.** Doctrine amendments rewrite the relevant section compactly — never
   append "AMENDMENT" blocks.
5. **Fold, don't pile.** When cap pressure appears: archive the settled material verbatim
   to memory/archive/ with a pointer line, then delete it from the live file.
6. **Expired mandates are removed** at the next rewrite. The archive preserves them;
   the prompt is not the historian.

## Anti-patterns (all observed in v1)
- "DEPLOYED <sha>" verification paragraphs living in the prompt weeks after the deploy.
- Settle-check instructions whose date passed, never removed.
- The same doctrine restated in three sections as amendments stacked up.
- Knowledge the node needs ~once/month injected every cycle at cache-create prices.

## When editing any node prompt
- Check `wc -c` against the declared cap BEFORE uploading; never ship a file that
  violates its own header.
- Verify the new bundle math by running the runner's injection block manually.
- If a requested addition doesn't fit: fold something settled, or refuse the addition
  and escalate — the cap is the point.
