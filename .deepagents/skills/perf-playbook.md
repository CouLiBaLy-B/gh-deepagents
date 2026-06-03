# Skill: perf-playbook

Empirical performance work. **Never optimise without numbers.**

## The loop

```
reproduce → baseline → profile → hypothesis → fix → validate
                  ▲                                       │
                  └───────────────── repeat ──────────────┘
```

## Required outputs at each step

| Step       | Must produce                                                  |
|------------|---------------------------------------------------------------|
| reproduce  | A single shell command that triggers the slow path            |
| baseline   | Median wall time over ≥5 runs (use `benchmark_run`)           |
| profile    | A flamegraph SVG path + textual top-10 hotspots               |
| hypothesis | "X% of time is spent in `f` because Y; fixing Z → ~W% gain"   |
| fix        | A patch (delegated to coder)                                  |
| validate   | `perf_compare` showing ≥10% improvement on the same command   |

If any step yields no actionable result, ABORT and document what you tried.

## Profiler choice

| Target                                 | Tool                      |
|----------------------------------------|---------------------------|
| Python long-running process / server   | `profile_python`          |
| Python short script                    | `cprofile_run`            |
| Async hot path                         | `profile_python` (--native if numpy/pandas) |
| Just need wall-time comparison         | `benchmark_run` / `perf_compare` |
| Memory bloat                           | Outside scope — hand back |

## Common Python anti-patterns to look for

- O(N²) loops over lists where `set` lookup would do.
- Repeated `pd.concat` in a loop (use list + single concat).
- `json.loads(s)` called on the same big payload twice.
- Regex compiled inside a hot loop.
- SQLAlchemy lazy-loading triggering N+1.
- `requests.get` without `Session()` reuse.
- `subprocess` with `shell=True` in a hot loop (fork is expensive).
- Synchronous I/O inside an `async` handler (use `run_in_executor`).

## How to present findings to the lead

```markdown
## Perf analysis: /search endpoint

- **Repro**: `curl -s localhost:8000/search?q=test | head`
- **Baseline**: 1.42s median (n=10, stdev 0.05s)
- **Top hotspot**: `app/search.py:84  rank_results()` — 78% cumulative time
- **Hypothesis**: the inner loop calls `model.embed(text)` per candidate;
  batching to a single call should drop wall time to ~0.3s (~78% saved).
- **Profile**: `.gh-deepagent-profile.svg` (open in browser)
- **Proposed fix**: pass candidates as a list to `model.embed`, then index.
```

## Hard limits

- Don't profile in CPU-throttled containers without flagging it.
- Don't trust a single measurement — always ≥5 runs.
- Don't celebrate a "10x speedup" on synthetic input — re-bench on realistic data.
