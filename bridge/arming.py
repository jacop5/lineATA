"""Experimental arming variants derived from the golden phys ritual.

The full 54-op ritual faults a live session (state-dependent tail); these
variants test which parts are state-independent:
  ARM_ALMOST:   ritual minus the init re-trigger (a0 03 01 / 80 03 01) and
                the trailing identity query (c2)
  ARM_NO_RESET: additionally without the mid-sequence port resets (07/27)
Derived offline from the golden capture 2026-11-23.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from handlers import on_phys_64 as _full

# index 49 = a00301 (init re-trigger), 50 = 800301, 53 = c202: excluded
ARM_ALMOST = _full[:49] + _full[51:53]
ARM_NO_RESET = [h for i, h in enumerate(_full) if i not in (24, 47, 49, 50, 53)]
