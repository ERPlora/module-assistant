"""
AI Assistant Tool Registry.

Provides the base class for all tools and auto-discovery of ai_tools.py
files across all active modules.

Each module that wants AI integration creates an ai_tools.py file
in its root directory with tools registered via @register_tool.
"""
import importlib
import logging

logger = logging.getLogger(__name__)

TOOL_REGISTRY = {}


class AssistantTool:
    """Base class for all AI assistant tools."""
    name: str = ''
    description: str = ''
    parameters: dict = {}
    module_id: str = None
    requires_confirmation: bool = False
    required_permission: str = None
    setup_only: bool = False
    examples: list = []

    def execute(self, args: dict, request) -> dict:
        raise NotImplementedError(f"Tool {self.name} must implement execute()")

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
        except Exception:
            raise  # Let unknown exceptions propagate for logging

    def to_openai_schema(self) -> dict:
        """Convert tool to OpenAI function calling schema."""
        desc = self.description
        if self.examples:
            import json
            examples_text = '\n'.join(
                f"  {json.dumps(ex, ensure_ascii=False)}"
                for ex in self.examples
            )
            desc += f"\n\nExamples:\n{examples_text}"
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


def discover_tools():
    """
    Discover and register tools from all active modules.

    1. Registers hub core tools (always available)
    2. Scans all active modules for ai_tools.py files
    """
    TOOL_REGISTRY.clear()

    # 1. Register hub core tools (always available)
    core_modules = [
        'assistant.tools.hub_tools',
        'assistant.tools.setup_tools',
        'assistant.tools.configure_tools',
        'assistant.tools.analytics_tools',
    ]
    for mod_name in core_modules:
        try:
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
        except ImportError as e:
            logger.error(f"[ASSISTANT] Failed to load {mod_name}: {e}")

    # 2. Discover ai_tools.py in each active module
    active_modules = _get_active_module_ids()

    for module_id in active_modules:
        if module_id == 'assistant':
            continue  # Skip self
        try:
            mod = importlib.import_module(f'{module_id}.ai_tools')
            importlib.reload(mod)
            logger.debug(f"[ASSISTANT] Loaded ai_tools from {module_id}")
        except ImportError:
            pass  # Module doesn't have ai_tools.py — normal, skip
        except Exception as e:
            logger.warning(f"[ASSISTANT] Error loading ai_tools from {module_id}: {e}")

    logger.info(f"[ASSISTANT] Discovered {len(TOOL_REGISTRY)} tools")


def get_tools_for_context(context='general', user=None):
    """
    Return OpenAI tool schemas filtered by context and user permissions.

    Args:
        context: 'general' or 'setup'
        user: LocalUser instance for permission filtering
    """
    if not TOOL_REGISTRY:
        discover_tools()

    active_modules = set(_get_active_module_ids())

    tools = []
    for tool in TOOL_REGISTRY.values():
        # Only include if the tool's module is active (or hub core)
        if tool.module_id and tool.module_id not in active_modules:
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
