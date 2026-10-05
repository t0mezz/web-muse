# AGENTS.md

## Code Map
- server - application source
- web - application source
- tests - automated tests

## Conventions
- Use `from ... import ...` for Python imports.
- Use `.js` extensions.
- Name Python tests `test_*.py`.

## Workflow
- Commit after every major update with a clear message.

## Service
- Restart the backend when server changes need it: `systemctl --user restart web-muse`.
