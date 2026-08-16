# Python client — PipeCraft 1.5

PipeCraft 1.5 treats Python strictly as a client. The package contains no graph scheduler, process supervisor, retry engine, resource manager, persistence engine, or plugin host.

The preferred execution path is the resident Rust service over `pipecraft.ipc/v1`:

```python
from pipecraft import PipeCraft

pc = PipeCraft("/workspace")
run_id = pc.submit("build", execute=True, max_workers=4)
job = pc.wait(run_id)
report = pc.report(run_id)
```

`run()` is convenience sugar over `submit()`, `wait()`, and `report()`. Closing Python does not cancel a submitted job.

The default Unix socket is `.pipelines/pipecraft.sock`; override it with `endpoint=` or `PIPECRAFT_ENDPOINT`. Discovery/static planning operations continue to call the Rust CLI because they do not need a resident scheduler.
