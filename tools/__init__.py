"""
AI Assistant Tool Registry.

Provides the base class for all tools and auto-discovery of ai_tools.py
files across all active modules.

Each module that wants AI integration creates an ai_tools.py file
in its root directory with tools registered via @register_tool.
"""
import copy
import importlib
import logging
import sys

logger = logging.getLogger(__name__)

TOOL_REGISTRY = {}


def _make_strict_schema(schema: dict) -> dict:
    """
    Recursively transform a JSON Schema dict for OpenAI strict mode:
    - For any object type (dict with "type": "object" or with "properties"):
        - Adds "additionalProperties": False
        - Sets "required" to the full list of keys in "properties"
    - Recurses into property values, "items", "$defs", and "definitions".
    Returns a deep copy — the original schema is never mutated.
    """
    schema = copy.deepcopy(schema)

    def _process(node):
        if not isinstance(node, dict):
            return node

        is_object = node.get('type') == 'object' or 'properties' in node

        if is_object:
            node['additionalProperties'] = False
            props = node.get('properties', {})
            if props:
                node['required'] = list(props.keys())
            else:
                node['required'] = []
            # Recurse into each property schema
            for key in props:
                props[key] = _process(props[key])

        # Recurse into array items
        if 'items' in node:
            node['items'] = _process(node['items'])

        # Recurse into $defs / definitions
        for defs_key in ('$defs', 'definitions'):
            if defs_key in node and isinstance(node[defs_key], dict):
                for def_name in node[defs_key]:
                    node[defs_key][def_name] = _process(node[defs_key][def_name])

        return node

    return _process(schema)


class AssistantTool:
    """Base class for all AI assistant tools."""
    name: str = ''
    description: str = ''
    short_description: str = ''  # Compact 1-line version sent to LLM. Falls back to description.
    parameters: dict = {}
    module_id: str = None
    strict: bool = True  # Set to False for tools with free-form object params
    requires_confirmation: bool = False
    required_permission: str = None
    setup_only: bool = False
    examples: list = []

    def execute(self, args: dict, request) -> dict:
        raise NotImplementedError(f"Tool {self.name} must implement execute()")

    def get_confirmation_data(self, args: dict, request) -> dict | None:
        """
        Return structured data to display in the confirmation UI.

        Override this in tools that benefit from showing rich context before
        the user confirms. Return None to show only the plain text description.

        The returned dict supports:
            title   (str)  — card header, e.g. "Cash Register Summary"
            rows    (list) — list of {"label": str, "value": str} pairs
            warning (str)  — optional warning line shown below the rows
            badge   (str)  — optional CSS color class for the header badge
                             (e.g. "color-warning", "color-error", "color-primary")
        """
        return None

    def safe_execute(self, args: dict, request) -> dict:
        """
        Wrapper around execute() that catches common exceptions
        and returns user-friendly error dicts instead of raising.
        """
        from django.core.exceptions import ObjectDoesNotExist, ValidationError
        try:
            return self.execute(args, request)
        except ObjectDoesNotExist as e:
            model_name = type(e).__qualname__.rsplit('.', 1)[0] if '.' in type(e).__qualname__ else 'Record'
            id_val = next((v for k, v in args.items() if 'id' in k.lower()), None)
            msg = f"{model_name} not found"
            if id_val:
                msg += f" (id: {id_val})"
            return {"error": msg}
        except ValidationError as e:
            messages = e.messages if hasattr(e, 'messages') else [str(e)]
            return {"error": f"Validation error: {'; '.join(messages)}"}
        except (ValueError, TypeError) as e:
            return {"error": f"Invalid input: {str(e)}"}
        except KeyError as e:
            return {"error": f"Missing required parameter: {e}"}
        except Exception:
            raise  # Let unknown exceptions propagate for logging

    def to_openai_schema(self) -> dict:
        """Convert tool to AI function calling schema (Responses API format).
        Uses short_description if set (saves tokens), falls back to description.
        Examples are appended only to the full description — not the short one.
        """
        desc = self.short_description or self.description
        if not self.short_description and self.examples:
            import json
            examples_text = '\n'.join(
                f"  {json.dumps(ex, ensure_ascii=False)}"
                for ex in self.examples
            )
            desc += f"\n\nExamples:\n{examples_text}"
        if self.strict:
            return {
                'type': 'function',
                'name': self.name,
                'description': desc,
                'strict': True,
                'parameters': _make_strict_schema(self.parameters),
            }
        return {
            'type': 'function',
            'name': self.name,
            'description': desc,
            'parameters': self.parameters,
        }


def register_tool(tool_cls):
    """Decorator to register a tool class in the global registry."""
    instance = tool_cls()
    if not instance.name:
        raise ValueError(f"Tool class {tool_cls.__name__} must define a 'name' attribute")
    TOOL_REGISTRY[instance.name] = instance
    return tool_cls


def _get_active_module_ids():
    """Get active module IDs by scanning the modules directory."""
    from django.conf import settings
    from pathlib import Path

    modules_dir = Path(settings.MODULES_DIR)
    module_ids = []
    if modules_dir.exists():
        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir():
                continue
            if module_dir.name.startswith('.') or module_dir.name.startswith('_'):
                continue
            module_ids.append(module_dir.name)
    return module_ids


def _re_register_from_module(mod):
    """Re-register all AssistantTool subclasses defined in a module."""
    import inspect
    for _name, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, AssistantTool) and obj is not AssistantTool and getattr(obj, 'name', ''):
            instance = obj()
            TOOL_REGISTRY[instance.name] = instance


def discover_tools():
    """
    Discover and register tools from all active modules.

    1. Registers hub core tools (always available)
    2. Scans all active modules for ai_tools.py files

    Uses import (not reload) for module ai_tools to avoid Django model
    re-registration errors. Already-imported modules are re-registered
    by scanning their AssistantTool subclasses.
    """
    TOOL_REGISTRY.clear()

    # 1. Register hub core tools (always available)
    core_modules = [
        'assistant.tools.hub_tools',
        'assistant.tools.setup_tools',
        'assistant.tools.configure_tools',
        'assistant.tools.analytics_tools',
        'assistant.tools.blueprint_tools',
        'assistant.tools.memory_tools',
        'assistant.tools.search_tools',
    ]
    for mod_name in core_modules:
        try:
            mod = importlib.import_module(mod_name)
            # Core tools are part of assistant — safe to reload
            importlib.reload(mod)
        except ImportError as e:
            logger.error(f"[ASSISTANT] Failed to load {mod_name}: {e}")

    # 2. Discover ai_tools.py in each active module
    active_modules = _get_active_module_ids()

    for module_id in active_modules:
        if module_id == 'assistant':
            continue  # Skip self
        try:
            ai_tools_name = f'{module_id}.ai_tools'
            if ai_tools_name in sys.modules:
                # Already imported — re-register tools without reloading
                # (reload can break Django model registry)
                _re_register_from_module(sys.modules[ai_tools_name])
            else:
                importlib.import_module(ai_tools_name)
            logger.debug(f"[ASSISTANT] Loaded ai_tools from {module_id}")
        except ImportError:
            pass  # Module doesn't have ai_tools.py — normal, skip
        except Exception as e:
            logger.warning(f"[ASSISTANT] Error loading ai_tools from {module_id}: {e}")

    logger.info(f"[ASSISTANT] Discovered {len(TOOL_REGISTRY)} tools")


def get_tools_for_context(context='general', user=None, loaded_modules=None):
    """
    Return tool schemas filtered by context and user permissions.

    Args:
        context: 'general' or 'setup'
        user: LocalUser instance for permission filtering
        loaded_modules: set of module IDs whose tools should be included.
            If None, all active module tools are included (legacy behavior).
            If a set, only hub core tools (module_id=None) and tools from
            the specified modules are included.
    """
    if not TOOL_REGISTRY:
        discover_tools()

    active_modules = set(_get_active_module_ids())

    tools = []
    for tool in TOOL_REGISTRY.values():
        # Only include if the tool's module is active (or hub core)
        if tool.module_id and tool.module_id not in active_modules:
            continue

        # Dynamic loading: if loaded_modules is set, filter non-core tools
        if loaded_modules is not None and tool.module_id:
            if tool.module_id not in loaded_modules:
                continue

        # Setup-only tools only in setup context
        if tool.setup_only and context != 'setup':
            continue

        # Permission check
        if user and tool.required_permission:
            if not user.has_perm(tool.required_permission):
                continue

        tools.append(tool.to_openai_schema())

    return tools


def get_tool(name: str):
    """Get a tool instance by name."""
    if not TOOL_REGISTRY:
        discover_tools()
    return TOOL_REGISTRY.get(name)


def resolve_module_dependencies(module_ids, active_modules=None):
    """
    Resolve module IDs to include their dependencies (transitive).

    Reads DEPENDENCIES from each module's module.py.
    Returns (resolved_ids, dep_map) where:
      - resolved_ids: set of all module IDs needed (requested + deps)
      - dep_map: {mid: [dep1, dep2]} for the modules that had deps added
    """
    if active_modules is None:
        active_modules = set(_get_active_module_ids())

    resolved = set()
    dep_map = {}

    def _resolve(mid):
        if mid in resolved:
            return
        resolved.add(mid)
        if mid not in active_modules:
            return
        try:
            import importlib
            mod = importlib.import_module(f'{mid}.module')
            deps = getattr(mod, 'DEPENDENCIES', []) or []
            if deps:
                dep_map[mid] = [d for d in deps if d not in module_ids]
            for dep in deps:
                _resolve(dep)
        except Exception:
            pass

    for mid in module_ids:
        _resolve(mid)

    return resolved, dep_map
