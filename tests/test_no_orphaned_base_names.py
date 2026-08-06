"""Permanent regression guard for the base<->tail cross-namespace bug class.

xobop_core_tail.py copies xobop_core_base.py's globals into itself at import
time (`for _k, _v in vars(_base).items(): globals()[_k] = _v`), but nothing
runs the reverse. A function *defined* in xobop_core_base.py always executes
with xobop_core_base's own module dict as its __globals__, regardless of who
calls it -- so if such a function references a name that only exists in
xobop_core_tail.py's namespace, it silently NameErrors at call time, not at
import time. This bit XoBop five times before this test existed (see git log
around 2026-08-05/06: evolve_blocks_recompute, the auto-improve autopromote
branch, and _run_agent/_run_checkup_sync/_generate_system_map_sync), always
swallowed by a surrounding try/except and only noticed by manually calling
the broken code path.

This test statically detects the pattern using bytecode LOAD_GLOBAL
instructions (precise -- unlike a raw AST Name walk, this can't be confused
by local variables, parameters, or attribute-access names, since Python
only emits LOAD_GLOBAL for actual global/builtin lookups).

Known heuristic limits (acceptable tradeoffs, not bugs in this test):
- Only checks functions that are direct attributes of the xobop_core_base
  module (`vars(xobop_core_base)`). Closures/nested functions defined
  inside another function aren't module-level attributes and are skipped.
- A name landing in the explicit ALLOWLIST below is trusted to actually be
  injected by xobop_core_tail.py -- if you add a new tail-only name without
  actually adding the corresponding `_base.X = X` injection, this test
  won't catch that (see test_cross_namespace_injections_actually_exist,
  which covers that half by importing both modules for real).
"""
import builtins
import dis
import types

import xobop_core_base as base

# Names xobop_core_tail.py injects into xobop_core_base's namespace after
# both modules finish importing (the `_base.X = X` assignments near the
# bottom of xobop_core_tail.py). This test imports xobop_core_base alone,
# before that injection happens, so these names are expected to be
# "missing" from vars(base) here -- that's not a bug, it's the documented
# mechanism. Keep this in sync with xobop_core_tail.py.
INJECTED_FROM_TAIL = {
    "_promote_proposal_sync",
    "_run_pipeline_sweep",
    "_cron_skill_handlers",
}

_BUILTIN_NAMES = set(dir(builtins))


def _global_names_referenced(func) -> set[str]:
    return {
        instr.argval
        for instr in dis.get_instructions(func.__code__)
        if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME")
    }


def _iter_module_level_functions(module):
    # Filter by __globals__ identity, not obj.__module__: xobop_core_tail.py's
    # own global-copy-on-import loop (`for _k,_v in vars(_base).items():
    # globals()[_k] = _v`) copies __name__ too, so every function later
    # defined in xobop_core_tail.py reports __module__ == "xobop_core_base"
    # even though it actually executes against xobop_core_tail's own
    # __globals__ dict. __globals__ identity is what actually determines
    # name resolution at call time, so it's the correct filter here.
    for name, obj in vars(module).items():
        if isinstance(obj, types.FunctionType) and obj.__globals__ is vars(module):
            yield name, obj


def test_no_orphaned_global_references_in_xobop_core_base():
    resolvable = set(vars(base)) | _BUILTIN_NAMES | INJECTED_FROM_TAIL

    orphaned: dict[str, set[str]] = {}
    for fname, func in _iter_module_level_functions(base):
        missing = _global_names_referenced(func) - resolvable
        if missing:
            orphaned[fname] = missing

    assert not orphaned, (
        "Function(s) in xobop_core_base.py reference names not resolvable in "
        "xobop_core_base's own module namespace, and not in this test's "
        "INJECTED_FROM_TAIL allowlist -- these will NameError at call time "
        f"unless xobop_core_tail.py injects them: {orphaned}"
    )


def test_cross_namespace_injections_actually_exist():
    """The other half of the guard: every name in INJECTED_FROM_TAIL must
    actually get injected by xobop_core_tail.py, or the allowlist above is
    silently hiding a real orphaned reference."""
    import xobop_core_tail  # noqa: F401  (import triggers the injection)

    missing = {name for name in INJECTED_FROM_TAIL if not hasattr(base, name)}
    assert not missing, (
        f"Names {missing} are allowlisted in INJECTED_FROM_TAIL but "
        "xobop_core_tail.py never actually injects them into xobop_core_base -- "
        "either the injection was removed, or the allowlist is stale."
    )
