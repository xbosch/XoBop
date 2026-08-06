import xobop_core_tail as _runtime

# See the __name__-clobbering comment in xobop_core_tail.py -- dunder
# attributes are module identity, not application state, and must not be
# copied through this compatibility-export chain.
for _k, _v in vars(_runtime).items():
    if _k.startswith("__"):
        continue
    globals()[_k] = _v
