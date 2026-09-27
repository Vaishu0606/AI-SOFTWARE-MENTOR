# Scripts

Developer utility scripts for the AI Software Mentor project.

## Available Scripts

| Script | Purpose | Status |
|---|---|---|
| `reset_demo_repo.py` | Restore the seeded discount bug in the **disposable** `ShopDemo` demo repository, so the hackathon demo is repeatable | Ready |

### `reset_demo_repo.py`

The mentor's approval gate writes the reviewed fix to the repository it was
given. A demo run against `ShopDemo` therefore leaves that checkout with the bug
already fixed, and the next run honestly concludes there is nothing to fix. Run
this before each demo to restore the seed condition.

```bash
# reset, then verify (run this before every demo)
python scripts/reset_demo_repo.py

# verify only, change nothing (exit 1 if not in the seeded state)
python scripts/reset_demo_repo.py --check
```

**Scope — this is deliberately narrow.** It rewrites exactly one file:

```
demo-repository/ShopDemo/app/services/discount_service.py
```

restoring the single line `return int(subtotal_cents / 100) * code.value`, and
nothing else. `ShopDemo` is a **disposable demo fixture** — not real customer
data, and it holds no secrets. The script is not a general-purpose "revert my
changes" tool, and it refuses to run if its resolved target would land outside
this repository's own `demo-repository/` directory.

It is deterministic, idempotent and self-verifying: it recognises only the two
known states of that line (the seeded bug and the mentor's fix), refuses to
guess at anything else, preserves the surrounding comment block and
indentation, and re-reads the file from disk to assert the exact seeded state
before reporting success. Standard library only — no dependencies, no network,
and no involvement from the backend.

Exit codes: `0` seeded state confirmed, `1` check failed or content
unrecognised, `2` target missing or refused by a safety guard.

## Planned Scripts

| Script | Purpose | Status |
|---|---|---|
| `setup.sh` / `setup.ps1` | Full project setup (install deps, create .env files) | Planned |
| `start-dev.sh` / `start-dev.ps1` | Start frontend + backend together | Planned |
| `run-tests.sh` | Run all backend and frontend tests | Planned |
| `reset-sessions.sh` | Clear local SQLite session data | Planned |
| `clone-demo-repo.sh` | Clone demo-repository into a local workspace session | Planned |
