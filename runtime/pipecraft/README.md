# PipeCraft runtime bundled with Styler

Styler 0.13.1 distributes a private PipeCraft runtime binary per supported Linux
architecture. The source repository does not need to vendor the PipeCraft source.

Expected layout for the x86_64 bundle:

    runtime/pipecraft/linux-x86_64/pipecraft

`install.sh` copies this binary into the isolated Styler release. Running Styler
directly from the extracted source tree also discovers this bundled runtime.
