---
name: travel-optimizer
description: China-first AI travel planner. Turns messy travel inputs (Xiaohongshu screenshots/copied text, attraction and restaurant lists, natural-language constraints) into feasible, explainable multi-day itineraries. Use whenever the user pastes travel recommendations or screenshots, shares a place list, or asks for a day-by-day trip plan.
---

# travel-optimizer (project skill entry)

This is a thin registration entry. The full skill instructions and the
Python package live in the `travel-optimizer/` directory at the repo root.

**Do this first:** read `travel-optimizer/SKILL.md` and follow it exactly -
it defines the full workflow (screenshot transcription, place extraction,
TripRequest classification, provider selection, planning, output format),
the decision hierarchy, and the feasibility/rating/transportation policies.

Quick orientation:

- Run everything from the `travel-optimizer/` directory (the modules use
  flat imports like `from models import ...`).
- No API key needed for offline planning: use `mock_maps.MockProvider`.
  For real China trips use `providers.AMapProvider` with the `AMAP_API_KEY`
  environment variable.
- Verify with `python3 -m pytest test_planner.py -q` (17 tests, all offline).
- Present results in the user's language (usually Chinese), translating the
  package's English reasons/reminders; keep place names as the user wrote them.
