#!/usr/bin/env python
"""The leakage probe lives in the package now: provenance/eval/leakage.py (`pace leakage`).

This path stays because CI and the nightly job invoke it. Same entry point, same flags.
"""

from __future__ import annotations

import sys

from provenance.eval.leakage import main

if __name__ == "__main__":
    sys.exit(main())
