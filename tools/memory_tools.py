"""
Memory Tools — always available (hub core).

Allows the AI assistant to persist facts across sessions by storing
key/value memories in the database. Memories are injected into every
system prompt so the assistant remembers them automatically.
"""
import logging

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class SaveMemory(AssistantTool):
    name = "save_memory"
    description = (
        "Save or update a persistent memory so it is remembered across all future sessions. "
        "Use a short, descriptive key (e.g. 'owner_name', 'lunch_menu', 'vip_customer_greeting'). "
        "If a memory with the same key already exists it will be overwritten. "
        "Use this when the user says 'remember that…' or provides info you should always recall."
    )
    short_description = "Save a persistent memory (key+content) that persists across sessions."
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short snake_case label for the memory (e.g. 'owner_name', 'usual_lunch_menu'). Max 200 chars.",
            },
            "content": {
                "type": "string",
                "description": "The full content to remember. Be specific and complete.",
            },
        },
        "required": ["key", "content"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from assistant.models import AssistantMemory
        from apps.configuration.models import HubConfig

        key = args.get("key", "").strip()
        content = args.get("content", "").strip()

        if not key:
            return {"error": "key is required"}
        if not content:
            return {"error": "content is required"}
        if len(key) > 200:
            return {"error": "key must be 200 characters or fewer"}

        hub_config = HubConfig.get_solo()
        hub_id = hub_config.hub_id

        memory, created = AssistantMemory.objects.update_or_create(
            hub_id=hub_id,
            key=key,
            defaults={"content": content},
        )

        action = "saved" if created else "updated"
        return {
            "success": True,
            "action": action,
            "key": key,
            "content": content,
        }


@register_tool
class GetMemories(AssistantTool):
    name = "get_memories"
    description = (
        "List all persistent memories stored for this hub. "
        "Returns all key/content pairs that have been saved across sessions."
    )
    short_description = "List all persistent memories for this hub."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from assistant.models import AssistantMemory
        from apps.configuration.models import HubConfig

        hub_config = HubConfig.get_solo()
        hub_id = hub_config.hub_id

        memories = AssistantMemory.objects.filter(
            hub_id=hub_id,
            is_deleted=False,
        ).order_by("key")

        result = [
            {
                "key": m.key,
                "content": m.content,
                "updated_at": m.updated_at.strftime("%Y-%m-%d %H:%M"),
            }
            for m in memories
        ]

        return {
            "memories": result,
            "count": len(result),
        }


@register_tool
class DeleteMemory(AssistantTool):
    name = "delete_memory"
    description = (
        "Delete a persistent memory by its key. "
        "Use this when the user asks to forget something previously remembered."
    )
    short_description = "Delete a persistent memory by key."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The key of the memory to delete.",
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from assistant.models import AssistantMemory
        from apps.configuration.models import HubConfig

        key = args.get("key", "").strip()
        if not key:
            return {"error": "key is required"}

        hub_config = HubConfig.get_solo()
        hub_id = hub_config.hub_id

        try:
            memory = AssistantMemory.objects.get(
                hub_id=hub_id,
                key=key,
                is_deleted=False,
            )
            memory.delete(hard_delete=True)
            return {"success": True, "deleted_key": key}
        except AssistantMemory.DoesNotExist:
            return {"error": f"No memory found with key '{key}'"}
