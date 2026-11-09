# WP-11 – Löwenstein adapter: feasibility research (post-v1)

**Depends on:** WP-03 (adapter contract proven) · **Parallel with:** anything

## Goal
Decide, with evidence, whether and how Löwenstein (prisma SMART/SOFT/20A/25ST, prismaLINE) data can be ingested through the existing adapter contract. Output is a research note + go/no-go, **not** an implementation.

## Scope
1. Inventory data paths: SD card (prisma SMART/SOFT, file types, encryption/obfuscation), prismaTS/prisma JOURNAL exports (CSV/PDF/XML?), cloud (prisma CLOUD API availability).
2. Survey existing open-source parsers: OSCAR loaders (`oscar/SleepLib/loader_plugins/` – check for prisma/Weinmann), other GitHub projects; licences.
3. Map available signals/events/settings onto the canonical model; list gaps (e.g. no oximetry, different event semantics).
4. Recommend: adapter design (detect/scan/load), required sample data, effort estimate, risks.

## Out of scope
Implementing the adapter (separate WP after go decision; it will need sample data – a new fixture set).

## Files
`docs/research/lowenstein.md` (new), new `docs/DECISIONS.md` entry if a go/no-go decision is taken.

## Acceptance
- Note cites sources (links, file names, licences) for every claim, distinguishes verified facts from assumptions.
- Clear go/no-go recommendation with the needed inputs from David (device model, sample data).
