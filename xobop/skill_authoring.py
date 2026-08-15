from __future__ import annotations

"""Self-authoring skills — xobop writes and hot-loads its own new capabilities.

A skill is just a Python function + a row in the `skills` table (see
skills_runtime.register_python_fn / xobop_core_base._upsert_skill). Every
extended-skill module (google_skills, browser_skill, media_skills) already
does this at startup for a fixed, human-written list. This module exposes
the same mechanism as a skill itself (`author_skill`) so the running agent
can define a brand new one at any time, with no restart:

  1. validate the name (valid identifier, not shadowing a reserved built-in)
     and the code (must parse, must define a top-level function matching name)
  2. write <name>.py + <name>.json (metadata sidecar: description/schema/
     risk_level/requires_force) into xobop/custom_skills/
  3. import the module fresh and register_python_fn(name, fn)
  4. upsert the DB row so /skills, _build_tools_from_db, etc. see it
     immediately

On startup, init() replays step 3+4 for every *.py already on disk, so
self-authored skills survive a restart without needing the LLM to re-author
them.
"""

import ast
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_CUSTOM_SKILLS_DIR = Path(__file__).parent / "custom_skills"

AUTHOR_SKILL_META: list[dict[str, Any]] = [
    {
        "name": "author_skill",
        "description": (
            "Write a brand new skill and make it callable immediately, with no restart. "
            "Use this whenever a task needs a capability that no existing skill covers "
            "(a new integration, a file format converter, a calculation, anything "
            "expressible as a self-contained Python function). code must define a "
            "top-level function named exactly `name`, either `def name(args: dict, **_) -> str` "
            "or `async def name(args: dict, **_) -> str` (may also return a "
            "(result:str, exit_code, status, artifact_path) 4-tuple like built-in skills do). "
            "It runs with full process privileges (same as run_shell) — whatever the Python "
            "environment can do, this can do. schema is the JSON Schema for the new skill's "
            "own arguments, exactly like any other skill's tool schema. Re-authoring an "
            "existing self-authored skill by name overwrites it (use this to fix a bug in "
            "a skill you wrote earlier)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case skill name, must be a valid Python identifier"},
                "description": {"type": "string", "description": "Shown to the model as the tool description"},
                "schema": {"type": "object", "description": "JSON Schema object for this new skill's arguments"},
                "code": {"type": "string", "description": "Full Python source defining the top-level function `name`"},
                "risk_level": {"type": "string", "enum": ["low", "normal", "high"], "description": "default normal"},
                "requires_force": {"type": "boolean", "description": "if true, callers must include the word 'force'. default false"},
                "timeout_sec": {"type": "integer", "description": "default 30"},
            },
            "required": ["name", "description", "schema", "code"],
        },
        "handler_type": "python_fn",
        "risk_level": "high",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 0,
        "visible_in_normal": 1,
    },
]


class SkillAuthoringError(Exception):
    pass


def _validate_name(name: str, *, reserved: set[str]) -> None:
    if not name.isidentifier():
        raise SkillAuthoringError(f"'{name}' is not a valid Python identifier")
    if name in reserved and not (_CUSTOM_SKILLS_DIR / f"{name}.py").exists():
        raise SkillAuthoringError(f"'{name}' is a reserved built-in skill name and cannot be created or overwritten this way")


def _validate_code(code: str, name: str) -> None:
    try:
        tree = ast.parse(code, filename=f"{name}.py")
    except SyntaxError as exc:
        raise SkillAuthoringError(f"code does not parse: {exc}") from exc
    top_level_defs = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if name not in top_level_defs:
        raise SkillAuthoringError(
            f"code must define a top-level function named exactly '{name}' "
            f"(found: {sorted(top_level_defs) or 'none'})"
        )


def _load_module_fn(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"xobop.custom_skills.{name}", path)
    if spec is None or spec.loader is None:
        raise SkillAuthoringError(f"could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, name, None)
    if fn is None or not callable(fn):
        raise SkillAuthoringError(f"module {path} has no callable '{name}' after import")
    return fn


def _make_author_skill(*, register_python_fn: Callable, upsert_skill: Callable, reserved_names: set[str]):
    async def author_skill(args: dict, **_) -> str:
        name = str(args["name"]).strip()
        description = str(args["description"]).strip()
        schema = args["schema"]
        code = args["code"]
        risk_level = str(args.get("risk_level", "normal"))
        requires_force = bool(args.get("requires_force", False))
        timeout_sec = int(args.get("timeout_sec", 30))

        if not isinstance(schema, dict):
            raise SkillAuthoringError("schema must be a JSON object")

        _validate_name(name, reserved=reserved_names)
        _validate_code(code, name)

        _CUSTOM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        py_path = _CUSTOM_SKILLS_DIR / f"{name}.py"
        meta_path = _CUSTOM_SKILLS_DIR / f"{name}.json"
        is_update = py_path.exists()

        py_path.write_text(code, encoding="utf-8")
        meta_path.write_text(json.dumps({
            "description": description,
            "schema": schema,
            "risk_level": risk_level,
            "requires_force": requires_force,
            "timeout_sec": timeout_sec,
        }, indent=2), encoding="utf-8")

        fn = _load_module_fn(py_path, name)
        register_python_fn(name, fn)
        upsert_skill(
            name=name, description=description, schema=schema, handler_type="python_fn",
            risk_level=risk_level, timeout_sec=timeout_sec, requires_force=requires_force,
            enabled=True, visible_in_strict=True, visible_in_normal=True,
        )

        verb = "Updated" if is_update else "Authored"
        return f"{verb} skill '{name}' and it is live now — call it like any other skill, no restart needed."

    return author_skill


def load_persisted_custom_skills(*, register_python_fn: Callable, upsert_skill: Callable) -> int:
    """Re-register every previously self-authored skill found on disk. Call once at startup."""
    if not _CUSTOM_SKILLS_DIR.exists():
        return 0
    count = 0
    for py_path in sorted(_CUSTOM_SKILLS_DIR.glob("*.py")):
        name = py_path.stem
        meta_path = py_path.with_suffix(".json")
        if not meta_path.exists():
            log.warning("Custom skill %s has no metadata sidecar (%s) — skipping", name, meta_path)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fn = _load_module_fn(py_path, name)
            register_python_fn(name, fn)
            upsert_skill(
                name=name, description=meta["description"], schema=meta["schema"], handler_type="python_fn",
                risk_level=meta.get("risk_level", "normal"), timeout_sec=meta.get("timeout_sec", 30),
                requires_force=meta.get("requires_force", False),
                enabled=True, visible_in_strict=True, visible_in_normal=True,
            )
            count += 1
        except Exception as exc:
            log.warning("Failed to reload custom skill '%s': %s", name, exc)
    return count


def init(*, register_python_fn: Callable, upsert_skill: Callable, reserved_names: set[str]) -> bool:
    try:
        register_python_fn("author_skill", _make_author_skill(
            register_python_fn=register_python_fn, upsert_skill=upsert_skill, reserved_names=reserved_names,
        ))
        n = load_persisted_custom_skills(register_python_fn=register_python_fn, upsert_skill=upsert_skill)
        log.info("Skill authoring loaded (author_skill); %d persisted custom skill(s) reloaded", n)
        return True
    except Exception as e:
        log.warning("Skill authoring init failed: %s", e)
        return False
