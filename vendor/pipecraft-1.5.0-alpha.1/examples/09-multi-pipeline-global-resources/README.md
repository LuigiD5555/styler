# Multi-pipeline global-resource example

From this directory, after building PipeCraft:

```sh
pipecraft run-many alpha beta \
  --execute \
  --max-pipelines 2 \
  --max-tasks 4 \
  --max-workers 2
```

`prepare` work from both pipelines may overlap. The two `critical` nodes may not
overlap because both request the same opaque global exclusive resource
`demo:critical-section`.
