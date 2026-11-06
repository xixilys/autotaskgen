# autotaskgen

A framework for running a research or engineering project with many agents:
- a dynamic task graph of routers and workers;
- an event log that every role writes (`progress`);
- a board;
- a patrol loop;
- node-level and project-level reviews;
- a project lead session.

It uses the Python standard library only. Most documentation is in Chinese.

## Layout

- Framework design: `docs/FRAMEWORK.md`, `docs/DESIGN.md`
- Project-level review (dossier): `docs/PROJECT_REVIEW.md`
- Node-level supervisor ("terra"): `docs/SUPERVISOR_TERRA_SPEC.md`
- Project lead: `docs/LEAD.md`. This is a long-lived agent session that:
  - acts on the numbers;
  - keeps a ledger of adjustments, each with a checkable `done_when` and a recheck time;
  - reports on the board.
- Nightly memory consolidation: `docs/DREAMING.md`
- Skills for routers and workers: `skills/`
- Example project declaration: `examples/example.project.json`
- Live snapshot format: `autotaskgen/LIVE_SCHEMA.md`

## Install

- Mac (CLI, snapshotter, patrol, nightly job): `sh autotaskgen/install_mac.sh`.
  - It copies the scripts to `~/.progress/bin` and loads the launchd jobs in `autotaskgen/launchd/`.
  - Edit the paths and environment in those plists first.
- Board host: copy `autotaskgen/` to `~/opt/progress/` and run `python3 server.py --port 8790`.
- Configuration is through environment variables:
  - `PROGRESS_HOME`, `PROGRESS_PROJECT`, `PROGRESS_REPO`, `PROGRESS_REMOTE` (ssh host of the board), `PROGRESS_REMOTE_CLI`;
  - `PROGRESS_PUSH_GUARD` (your publish-guard script);
  - `PROGRESS_TOOLING_HANDLE`, `ORCA_BIN`, `HERMES_BIN`;
  - see the top of each script for the rest.
- Optional QQ notifications through a Hermes gateway: `autotaskgen/hermes_plugin/progress-qq`. The target comes from `~/.progress/qq.json`.

## Tests

```
cd autotaskgen && python3 -m unittest discover -s tests
```

Tests use fakes only: no models, ssh, Orca or GitHub calls.

## License

MIT
