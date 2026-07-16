# MiniQueue fixture

MiniQueue is a deliberately small, standard-library-only job queue used by the
RDO light bench. It is application code, not benchmark machinery. The package
supports persistent jobs, leases, retries, deterministic scheduling, and
summary reporting.

Run the visible suite with:

```bash
python3 -m unittest discover -s tests -v
```

Benchmark setup must copy this directory, apply a case patch, and only then
create the disposable Git repository. The fixture history must not expose the
unpatched base to the worker.
