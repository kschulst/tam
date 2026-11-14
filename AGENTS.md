# tam

Run coding agents in tmux and get every session back after a reboot. Python 3.11+, standard library only (no runtime dependencies). See `README.md` for what it does and `docs/how-it-works.md` for how.

## Development

- Tasks are in `justfile`; `just` lists them. `just check` (lint and tests) must pass before every commit.
- `just dev-on` makes `tam` and the tmux keys run this checkout; `just dev-off` goes back to Homebrew.
- Tests run against an isolated tmux server (`TMUX_TMPDIR`). Never start or touch the user's own tmux server from a test.
- tam is glue: prefer wiring existing tools (tmux, fzf, the agents' own CLIs) over writing new mechanisms.

## Commits

Conventional Commits. `.githooks/commit-msg` rejects anything else (`just setup` enables it).

```
feat: ⌥ t opens a plain terminal with the directory picker
fix(sidebar): closing the current session no longer detaches the client
docs: explain why tam does not use tmux-resurrect
chore: release notes from Conventional Commits
```

- `feat`, `fix` and `perf` are what users see. Their subjects become the release notes word for word, so write them for users of tam, not for developers.
- Everything else (`docs`, `chore`, `refactor`, `test`, `build`, `ci`, `style`) is left out of the notes.
- Never mix a user-facing change and docs or tooling in one commit. Split them.
- A breaking change: `feat!: …`, or `BREAKING CHANGE:` in the body.

## Releases

Only when the user asks: `just release`. The version follows from the commits since the last tag (`feat` → minor, `fix`/`perf` → patch). It tags, pushes, creates the GitHub Release, updates `CHANGELOG.md` and the formula in `kschulst/homebrew-tap`.
