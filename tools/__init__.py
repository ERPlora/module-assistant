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

    def execute(self, args: dict, request) -> dict:
        raise NotImplementedError(f"Tool {self.name} must implement execute()")

    def to_openai_schema(self) -> dict:
        """Convert tool to OpenAI function calling schema."""
        return {
            'type': 'function',
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
            'strict': True,
        }


def register_tool(tool_cls):
    """Decorator to register a tool class in the global registry."""
    instance = tool_cls()
    if not instance.name:
        raise ValueError(f"Tool class {tool_cls.__name__} must define a 'name' attribute")
    TOOL_REGISTRY[instance.name] = instance
    return tool_cls


def discover_tools():
    """
    Discover and register tools from all active modules.

    1. Registers hub core tools (always available)
    2. Scans all active modules for ai_tools.py files
    """
    TOOL_REGISTRY.clear()

    # 1. Register hub core tools (always available)
    try:
        from assistant.tools import hub_tools  # noqa: F401
        from assistant.tools import setup_tools  # noqa: F401
    except ImportError as e:
        logger.error(f"[ASSISTANT] Failed to load core tools: {e}")

    # 2. Discover ai_tools.py in each active module
    try:
        from apps.modules_runtime.loader import module_loader
        active_modules = list(module_loader.loaded_modules.keys())
    except ImportError:
        active_modules = []

    for module_id in active_modules:
        if module_id == 'assistant':
            continue  # Skip self
        try:
            importlib.import_module(f'{module_id}.ai_tools')
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

    try:
        from apps.modules_runtime.loader import module_loader
        active_modules = set(module_loader.loaded_modules.keys())
    except ImportError:
        active_modules = set()

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
