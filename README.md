# harness

A file-based discipline harness for Claude Code agents: deterministic hooks
(fail-open gates), statutory file-based memory, a daily sentinel, and an LLM
judge isolated from what it measures. Discipline is enforced by mechanisms,
not promised by the model.

**Status: private, pre-release.** Public at `v0.1.0` after the adversarial
scrub passes. Full documentation lands with the docs batch.

## Layout

| Directory | Contents |
|---|---|
| `hooks/` | Core hook helper + cross-cutting gates |
| `shield/` | 3-layer behavioral rule enforcement |
| `memory/` | Statutory memory format + memory-verdict-gate |
| `recall/` | "Already built" artifact catalog engine |
| `sentinel/` | Daily self-discovering health checks |
| `governor/` | Gate governance (two adversarial LLM judges) |
| `watch/` | Read-only observation panel (schema + indexer + analyst) |
| `launchers/` | Agent launcher templates (vault → env → claude) |
| `docs/` | Architecture, quickstart, one page per module |
| `tests/` | Test suites (they travel with the code) |

## License

Apache-2.0 — see [LICENSE](LICENSE).
