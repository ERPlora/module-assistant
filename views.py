"""
AI Assistant Views.

Handles chat page rendering, message processing with agentic loop,
and action confirmation. Supports HTMX polling for streaming progress.
"""
import base64
import hashlib
import json
import logging
import threading
import uuid as uuid_mod

from django.core.cache import cache
from django.http import HttpResponse, StreamingHttpResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from django.utils.translation import gettext as _

from apps.accounts.decorators import login_required, permission_required
from apps.modules_runtime.navigation import with_module_nav
from apps.core.htmx import htmx_view

from .models import (
    AssistantConversation, AssistantActionLog, AssistantMessage,
    AssistantRequest, AssistantFile,
)
from .prompts import build_system_prompt
from .tools import get_tools_for_context, get_tool
from .feedback import record_feedback

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10
MAX_IDENTICAL_CALLS = 2  # max times same tool+args can repeat
PROGRESS_CACHE_TIMEOUT = 600  # seconds (10 min — blueprint installs can take 3-5 min)


def _validate_tool_args(tool, tool_args):
    """
    Validate tool arguments against the tool's JSON Schema.

    Returns None if valid, or a descriptive error string if invalid.
    """
    schema = tool.parameters
    if not schema:
        return None

    # Check required fields
    required = schema.get('required', [])
    missing = [f for f in required if f not in tool_args]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"

    # Check for unknown fields when additionalProperties is false
    if not schema.get('additionalProperties', True):
        allowed = set(schema.get('properties', {}).keys())
        unknown = [f for f in tool_args if f not in allowed]
        if unknown:
            return f"Unknown fields: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}"

    # Validate types for provided fields
    properties = schema.get('properties', {})
    for field, value in tool_args.items():
        prop_schema = properties.get(field)
        if not prop_schema:
            continue
        expected_type = prop_schema.get('type')
        if not expected_type:
            continue
        # Handle union types like ["string", "null"]
        if isinstance(expected_type, list):
            if value is None and 'null' in expected_type:
                continue
            real_types = [t for t in expected_type if t != 'null']
            expected_type = real_types[0] if real_types else None
            if not expected_type:
                continue
        # Basic type check (covers most tool schemas)
        type_map = {
            'string': str, 'integer': int, 'number': (int, float),
            'boolean': bool, 'array': list, 'object': dict,
        }
        py_type = type_map.get(expected_type)
        if py_type and not isinstance(value, py_type):
            return f"Field '{field}' must be {expected_type}, got {type(value).__name__}"

    return None


def _call_hash(tool_name, tool_args):
    """Deterministic hash of a tool call for loop detection."""
    key = json.dumps({'t': tool_name, 'a': tool_args}, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


class _UUIDEncoder(json.JSONEncoder):
    """JSON encoder that converts UUID objects to strings."""
    def default(self, obj):
        if isinstance(obj, uuid_mod.UUID):
            return str(obj)
        return super().default(obj)


def _strip_none(obj):
    """Recursively remove None values from nested structures.

    Gemini's API rejects both null and empty-string values in structs
    ('Value is not a struct: null' / 'Value is not a struct: ""').
    This removes None-valued keys from dicts entirely.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            v2 = _strip_none(v)
            if v2 is not None:
                cleaned[k] = v2
        return cleaned
    if isinstance(obj, list):
        result = []
        for v in obj:
            v2 = _strip_none(v)
            if v2 is not None:
                result.append(v2)
        return result
    return obj


def _json_dumps(obj):
    """json.dumps with UUID support + null sanitization for Gemini."""
    cleaned = _strip_none(obj)
    if cleaned is None:
        cleaned = {}
    return json.dumps(cleaned, cls=_UUIDEncoder)


# ============================================================================
# PAGE VIEWS
# ============================================================================

def _get_tier_info(hub_jwt):
    """Fetch assistant tier info from Cloud. Returns dict with features, tier slug, etc."""
    default = {'features': [], 'tier': 'free', 'tier_name': 'Free'}
    try:
        import requests as http_requests
        from django.conf import settings
        base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')
        resp = http_requests.get(
            f"{base_url}/api/hubs/me/assistant/config/",
            headers={'Authorization': f'Bearer {hub_jwt}'},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                'features': data.get('features', []),
                'tier': data.get('tier', 'free'),
                'tier_name': data.get('tier_name', 'Free'),
            }
    except Exception:
        pass
    return default


def _group_conversations_by_date(conversations, today):
    """Group conversations into (label, items) tuples for WhatsApp-style date headers."""
    from datetime import timedelta
    from django.utils.translation import gettext as _

    yesterday = today - timedelta(days=1)
    groups = []
    current_label = None
    current_items = []

    for conv in conversations:
        conv_date = conv.updated_at.date() if hasattr(conv.updated_at, 'date') else conv.updated_at
        if conv_date == today:
            label = _("Today")
        elif conv_date == yesterday:
            label = _("Yesterday")
        else:
            label = conv_date.strftime("%d/%m/%Y")

        if label != current_label:
            if current_items:
                groups.append((current_label, current_items))
            current_label = label
            current_items = [conv]
        else:
            current_items.append(conv)

    if current_items:
        groups.append((current_label, current_items))

    return groups


@login_required
@permission_required('assistant.use_chat')
@with_module_nav('assistant', 'chat')
@htmx_view('assistant/pages/chat.html', 'assistant/partials/chat_panel.html')
def chat_page(request):
    """Main chat page — no sidebar, messages load via infinite scroll up."""
    # Restore a specific conversation or the most recent one
    requested_id = request.GET.get('conversation_id')
    active_conversation = None

    if requested_id:
        try:
            active_conversation = AssistantConversation.objects.get(
                id=int(requested_id),
                user_id=request.session.get('local_user_id'),
            )
        except (AssistantConversation.DoesNotExist, ValueError, TypeError):
            pass

    if not active_conversation:
        active_conversation = (
            AssistantConversation.objects.filter(
                user_id=request.session.get('local_user_id'),
            ).order_by('-updated_at').first()
        )

    # Detect setup context (redirected from StoreConfigCheckMiddleware)
    from apps.configuration.models import HubConfig
    hub_config = HubConfig.get_config()
    context = 'setup' if (request.GET.get('context') == 'setup' or not hub_config.is_configured) else 'general'

    # Get tier info to show/hide UI elements and upgrade link
    tier_info = _get_tier_info(hub_config.hub_jwt)
    tier_features = tier_info['features']
    can_attach = 'files' in tier_features or 'images' in tier_features
    tier_slug = tier_info['tier']

    # Build upgrade URL (Cloud dashboard)
    from django.conf import settings
    cloud_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')
    show_upgrade = tier_slug != 'enterprise'
    upgrade_url = f"{cloud_url}/dashboard/assistant/upgrade/" if show_upgrade else ''

    return {
        'last_conversation': active_conversation,
        'chat_context': context,
        'is_setup_mode': context == 'setup',
        'can_attach': can_attach,
        'restore_conversation_id': active_conversation.id if active_conversation else None,
        'tier_slug': tier_slug,
        'show_upgrade': show_upgrade,
        'upgrade_url': upgrade_url,
    }


@login_required
@permission_required('assistant.view_logs')
def history_load_more(request):
    """HTMX endpoint: load more conversations for history (infinite scroll + search)."""
    PAGE_SIZE = 20
    try:
        offset = int(request.GET.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    search = request.GET.get('q', '').strip()

    qs = AssistantConversation.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).order_by('-updated_at')

    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(title__icontains=search) |
            Q(first_message__icontains=search) |
            Q(summary__icontains=search)
        )

    conversations = list(qs[offset:offset + PAGE_SIZE + 1])
    has_more = len(conversations) > PAGE_SIZE
    conversations = conversations[:PAGE_SIZE]
    next_offset = offset + PAGE_SIZE if has_more else None

    from django.utils import timezone
    today = timezone.localdate()
    grouped = _group_conversations_by_date(conversations, today)

    from django.shortcuts import render as django_render
    return django_render(request, 'assistant/partials/conversation_items.html', {
        'grouped_conversations': grouped,
        'next_offset': next_offset,
        'search_query': search,
    })


@login_required
@permission_required('assistant.view_logs')
@with_module_nav('assistant', 'history')
@htmx_view('assistant/pages/history.html', 'assistant/partials/conversations_content.html')
def history_page(request):
    """Conversation history — WhatsApp-style list with search and date groups."""
    PAGE_SIZE = 20
    try:
        offset = int(request.GET.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    search = request.GET.get('q', '').strip()

    qs = AssistantConversation.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).order_by('-updated_at')

    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(title__icontains=search) |
            Q(first_message__icontains=search) |
            Q(summary__icontains=search)
        )

    conversations = list(qs[offset:offset + PAGE_SIZE + 1])
    has_more = len(conversations) > PAGE_SIZE
    conversations = conversations[:PAGE_SIZE]
    next_offset = offset + PAGE_SIZE if has_more else None

    from django.utils import timezone
    today = timezone.localdate()
    grouped = _group_conversations_by_date(conversations, today)

    # For HTMX infinite scroll requests, return only the items
    if request.headers.get('HX-Request') and offset > 0:
        from django.shortcuts import render as django_render
        return django_render(request, 'assistant/partials/conversation_items.html', {
            'grouped_conversations': grouped,
            'next_offset': next_offset,
            'search_query': search,
        })

    return {
        'grouped_conversations': grouped,
        'next_offset': next_offset,
        'search_query': search,
    }


@login_required
@permission_required('assistant.use_chat')
def load_conversation_messages(request, conversation_id):
    """
    Load conversation messages with pagination (infinite scroll up).

    Returns newest messages first in pages of MESSAGES_PAGE_SIZE.
    The HTML is reversed so messages display in chronological order.
    Includes a sentinel div for loading older messages when ?before_id is set.

    Query params:
        before_id: load messages older than this message ID
    """
    import markdown

    MESSAGES_PAGE_SIZE = 10

    try:
        before_id = int(request.GET.get('before_id', 0))
    except (ValueError, TypeError):
        before_id = 0

    qs = AssistantMessage.objects.filter(
        conversation_id=conversation_id,
    ).order_by('-created_at')

    if before_id:
        qs = qs.filter(id__lt=before_id)

    messages_page = list(qs[:MESSAGES_PAGE_SIZE + 1])
    has_older = len(messages_page) > MESSAGES_PAGE_SIZE
    messages_page = messages_page[:MESSAGES_PAGE_SIZE]

    if not messages_page and not before_id:
        # No local messages — try Cloud fallback for first load
        cloud_messages = _fetch_messages_from_cloud(conversation_id)
        if cloud_messages:
            return _render_all_messages(cloud_messages)
        return HttpResponse('')

    # Reverse to chronological order for rendering
    messages_page.reverse()

    md_renderer = markdown.Markdown(extensions=['tables', 'fenced_code'])
    html_parts = []

    # Sentinel for loading older messages
    if has_older:
        oldest_id = messages_page[0].id
        html_parts.append(
            f'<div id="chat-load-older" '
            f'data-before-id="{oldest_id}" '
            f'class="flex items-center justify-center p-2">'
            f'<span class="loading loading-sm"></span>'
            f'</div>'
        )

    for msg in messages_page:
        if msg.role == 'assistant' and msg.content:
            rendered = md_renderer.convert(msg.content)
            md_renderer.reset()
            html_parts.append(render_to_string('assistant/partials/message.html', {
                'role': 'assistant',
                'content': rendered,
            }))
        elif msg.role == 'user' and msg.content:
            html_parts.append(render_to_string('assistant/partials/message.html', {
                'role': 'user',
                'content': content if (content := msg.content) else '',
            }))

    return HttpResponse(''.join(html_parts))


def _render_all_messages(messages_list):
    """Render a full list of (role, content) tuples as HTML (no pagination)."""
    import markdown
    md_renderer = markdown.Markdown(extensions=['tables', 'fenced_code'])
    html_parts = []
    for role, content in messages_list:
        if role == 'assistant' and content:
            rendered = md_renderer.convert(content)
            md_renderer.reset()
            html_parts.append(render_to_string('assistant/partials/message.html', {
                'role': 'assistant',
                'content': rendered,
            }))
        elif role == 'user' and content:
            html_parts.append(render_to_string('assistant/partials/message.html', {
                'role': 'user',
                'content': content,
            }))
    return HttpResponse(''.join(html_parts))


def _fetch_messages_from_cloud(conversation_id):
    """Fetch messages from Cloud API. Returns list of (role, content) tuples."""
    import requests as http_requests
    from apps.configuration.models import HubConfig

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        return []

    from django.conf import settings as django_settings
    base_url = getattr(django_settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')

    try:
        response = http_requests.get(
            f"{base_url}/api/hubs/me/assistant/history/{conversation_id}/",
            headers={
                'Authorization': f'Bearer {config.hub_jwt}',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        if response.status_code != 200:
            return []

        data = response.json()
        messages = []
        for msg in data.get('messages', []):
            role = msg.get('role')
            content = msg.get('content', '')
            if role in ('user', 'assistant') and content:
                messages.append((role, content))
        return messages

    except Exception as e:
        logger.warning(f"[ASSISTANT] Failed to load conversation {conversation_id} from Cloud: {e}")
        return []


@login_required
@permission_required('assistant.view_logs')
@with_module_nav('assistant', 'logs')
@htmx_view('assistant/pages/logs.html', 'assistant/partials/logs_content.html')
def logs_page(request):
    """Action log page."""
    logs = AssistantActionLog.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).select_related('conversation').order_by('-created_at')[:100]

    return {
        'logs': logs,
    }


# ============================================================================
# SHARED AGENTIC LOOP
# ============================================================================

class CloudProxyError(Exception):
    """Custom exception for Cloud proxy errors with status code."""
    def __init__(self, message, status_code=None, error_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_data = error_data or {}


class AgenticLoopError(Exception):
    """Error during the agentic loop, with a user-facing message."""
    pass


def _set_progress(request_id, event_type, data='', db_request_id=None):
    """Update progress for a polling request. Dual-write: cache + DB."""
    if request_id:
        cache.set(
            f'assistant_progress_{request_id}',
            {'type': event_type, 'data': data},
            timeout=PROGRESS_CACHE_TIMEOUT,
        )
    # Also persist to DB (survives hub restart)
    if db_request_id:
        try:
            updates = {'progress_message': str(data)[:200]}
            if event_type == 'complete':
                updates['status'] = 'complete'
            elif event_type == 'error':
                updates['status'] = 'error'
                updates['error_message'] = str(data)
            elif event_type in ('thinking', 'tool'):
                updates['status'] = 'processing'
            AssistantRequest.objects.filter(id=db_request_id).update(**updates)
        except Exception:
            pass  # Best-effort DB write


def _get_post_restart_message(user_id):
    """Build a user-friendly message when poll_progress finds empty cache (hub restarted).

    Checks the last confirmed action to tell the user what happened instead of
    the unhelpful generic message.
    """
    try:
        last_action = AssistantActionLog.objects.filter(
            user_id=user_id,
            confirmed=True,
        ).order_by('-created_at').first()

        if last_action and last_action.tool_name == 'execute_plan':
            result = last_action.result or {}
            installed = result.get('modules_installed', 0) or result.get('succeeded', 0)
            if last_action.success and installed > 0:
                return _(
                    "Se instalaron los módulos correctamente. "
                    "El sistema se reinició para cargarlos. "
                    "Envía un nuevo mensaje para continuar donde lo dejaste."
                )
            elif result.get('errors'):
                return _(
                    "Los módulos se instalaron parcialmente. El sistema se reinició para cargarlos. "
                    "Envía un nuevo mensaje para verificar el estado."
                )

        if last_action and last_action.success:
            return _(
                "El sistema se reinició tras completar la acción. "
                "Tus cambios se guardaron. Envía un nuevo mensaje para continuar."
            )
    except Exception:
        pass

    return _(
        "El sistema se reinició mientras procesaba tu solicitud. "
        "Tus cambios probablemente se guardaron. Envía un nuevo mensaje para continuar."
    )


def _check_db_request_status(request):
    """Check DB for the most recent pending/processing AssistantRequest for this user."""
    user_id = request.session.get('local_user_id')
    if not user_id:
        return None
    return AssistantRequest.objects.filter(
        user_id=user_id,
        status__in=['pending', 'processing', 'complete', 'error'],
    ).order_by('-created_at').first()


def run_agentic_loop(user, conversation, ai_input, context, request,
                     request_id=None, db_request_id=None):
    """
    Run the agentic tool-calling loop (polling/async path).

    Used by the HTMX chat view (file uploads) and plan resume flows.
    For text-only messages, chat_stream handles the agentic loop via SSE.

    Args:
        user: LocalUser instance
        conversation: AssistantConversation instance
        ai_input: str or list (text message or multimodal input)
        context: 'general' or 'setup'
        request: Django request (for session, building prompts)
        request_id: optional ID for progress tracking via polling
        db_request_id: optional AssistantRequest UUID for DB persistence

    Returns:
        dict with keys:
            response_text: str - The assistant's text response
            pending_actions: list of dicts with log_id, tool_name, tool_args, description
            conversation_id: int
            tier_info: dict or None
    """
    user_id = str(user.id)
    original_message = ai_input if isinstance(ai_input, str) else ''

    # Dynamic tool loading: restore from session so loaded state persists between messages.
    # Only keep modules that are still active (prevents stale refs after uninstall).
    from assistant.tools import (
        _get_active_module_ids, VIRTUAL_MODULES,
        preload_modules_for_message, resolve_module_dependencies,
    )
    active_module_ids = set(_get_active_module_ids())
    conversation_id = str(conversation.id)
    is_new_session = conversation.message_count == 0

    # On new conversation, clear any stale loaded_modules from previous session.
    if is_new_session:
        request.session.pop('assistant_loaded_modules', None)
        loaded_modules = set()
    else:
        session_loaded = request.session.get('assistant_loaded_modules', [])
        loaded_modules = set(session_loaded) & active_module_ids

    # Pre-load modules based on message keywords (eliminates extra LLM round-trip)
    if original_message:
        preload = preload_modules_for_message(original_message, active_module_ids, loaded_modules)
        if preload:
            # Resolve dependencies for real modules (not virtual)
            real_preload = {m for m in preload if m not in VIRTUAL_MODULES}
            if real_preload:
                resolved, _ = resolve_module_dependencies(real_preload, active_module_ids)
                preload = preload | resolved
            loaded_modules |= preload
            request.session['assistant_loaded_modules'] = list(loaded_modules)
            if hasattr(request.session, 'modified'):
                request.session.modified = True

    user_role = request.session.get('user_role', 'employee')
    tools = get_tools_for_context(context, user, loaded_modules=loaded_modules,
                                  user_role=user_role)

    # Match SOP workflow for this message
    from assistant.tools import match_sop, load_module_sops, SOP_REGISTRY
    matched_sop = None
    if original_message:
        # Load SOPs for pre-loaded modules
        for mid in loaded_modules:
            load_module_sops(mid)
        matched_sop = match_sop(original_message)

    # Build system prompt with dynamic sections based on message content
    instructions = build_system_prompt(request, context, message=original_message,
                                       is_new_session=is_new_session, matched_sop=matched_sop)

    response_text = ""
    pending_actions = []
    tier_info = None
    call_counts = {}  # {call_hash: count} for anti-loop detection

    _set_progress(request_id, 'thinking', _('Analizando tu solicitud...'),
                  db_request_id=db_request_id)

    use_async = _is_async_available()

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            # Use pre-fetched SSE response on first iteration to avoid double LLM call
            if prefetched_response is not None and iteration == 0:
                response_data = prefetched_response
                loop_tier_info = None
                prefetched_response = None  # consume it
            elif use_async:
                response_data, loop_tier_info = _call_cloud_async_with_poll(
                    request=request,
                    input_data=ai_input,
                    instructions=instructions,
                    tools=tools,
                    conversation_id=conversation_id,
                    new_session=(is_new_session and iteration == 0),
                    request_id=request_id,
                    db_request_id=db_request_id,
                )
            else:
                response_data, loop_tier_info = _call_cloud_proxy(
                    request=request,
                    input_data=ai_input,
                    instructions=instructions,
                    tools=tools,
                    conversation_id=conversation_id,
                    new_session=(is_new_session and iteration == 0),
                )
            if loop_tier_info:
                tier_info = loop_tier_info
        except CloudProxyError as e:
            logger.error(f"[ASSISTANT] Cloud proxy error: {e}")
            if e.status_code == 403:
                raise AgenticLoopError(
                    "AI Assistant subscription required. Please subscribe via the marketplace."
                )
            if e.status_code == 429:
                limit = e.error_data.get('limit', '')
                used = e.error_data.get('used', '')
                raise AgenticLoopError(
                    f"Monthly usage limit reached ({used}/{limit} sessions). "
                    "Please upgrade your plan or wait until next month."
                )
            # If async endpoint not found (404), fall back to sync for this session
            if use_async and e.status_code == 404:
                use_async = False
                response_data, loop_tier_info = _call_cloud_proxy(
                    request=request,
                    input_data=ai_input,
                    instructions=instructions,
                    tools=tools,
                    conversation_id=conversation_id,
                    new_session=(is_new_session and iteration == 0),
                )
                if loop_tier_info:
                    tier_info = loop_tier_info
            else:
                raise AgenticLoopError(f"Error connecting to AI service: {str(e)}")
        except AgenticLoopError:
            raise
        except Exception as e:
            logger.error(f"[ASSISTANT] Cloud proxy error: {e}")
            raise AgenticLoopError(f"Error connecting to AI service: {str(e)}")

        if not response_data:
            raise AgenticLoopError("No response from AI service")

        # Conversation history is managed by Cloud proxy (keyed by conversation_id)
        response_id = response_data.get('id', '')

        # Extract output items
        output = response_data.get('output', [])

        text_parts = []
        function_calls = []

        for item in output:
            if item.get('type') == 'message':
                for content in item.get('content', []):
                    if content.get('type') == 'output_text':
                        text_parts.append(content.get('text', ''))
            elif item.get('type') == 'function_call':
                function_calls.append(item)

        if text_parts:
            response_text = '\n'.join(text_parts)

        # If no function calls, we're done
        if not function_calls:
            break

        # Execute function calls
        tool_results = []
        has_pending = False
        pending_llm_message_id = ''  # shared id for all pending actions from this LLM message

        for fc in function_calls:
            tool_name = fc.get('name', '')
            call_id = fc.get('call_id', '')
            try:
                tool_args = json.loads(fc.get('arguments', '{}'))
            except json.JSONDecodeError:
                logger.warning(f"[ASSISTANT] Malformed JSON args for {tool_name}: {fc.get('arguments', '')[:200]}")
                tool_results.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': _json_dumps({"error": f"Malformed JSON arguments for {tool_name}. Please send valid JSON."}),
                })
                continue

            # Wrap the entire tool dispatch in try/except so that
            # every call_id always gets a result (prevents BUG-002:
            # "No tool output found for function call" corruption).
            try:
                _set_progress(request_id, 'tool', _('Usando %(tool)s...') % {'tool': tool_name},
                              db_request_id=db_request_id)
                tool = get_tool(tool_name)
                if not tool:
                    record_feedback(
                        event_type='missing_feature',
                        user=user,
                        conversation=conversation,
                        tool_name=tool_name,
                        user_message=original_message,
                        details={'tool_args': tool_args},
                    )
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({"error": f"Unknown tool: {tool_name}"}),
                    })
                    continue

                # Permission check
                if tool.required_permission and not user.has_perm(tool.required_permission):
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({"error": f"Permission denied: {tool.required_permission}"}),
                    })
                    continue

                # Employee read-only guard (hard block — even if LLM hallucinates a write tool)
                from assistant.tools import is_read_only_tool
                if user_role == 'employee' and not is_read_only_tool(tool_name):
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({
                            "error": "Read-only access. Your role (employee) can only query data. "
                            "Ask a manager or admin to make changes."
                        }),
                    })
                    continue

                # Anti-loop: detect identical tool calls
                ch = _call_hash(tool_name, tool_args)
                call_counts[ch] = call_counts.get(ch, 0) + 1
                if call_counts[ch] > MAX_IDENTICAL_CALLS:
                    logger.warning(f"[ASSISTANT] Loop detected: {tool_name} called {call_counts[ch]} times with same args")
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({
                            "error": f"You already called {tool_name} with these same arguments. "
                            "Use different parameters or proceed with the information you have."
                        }),
                    })
                    continue

                # Schema validation
                validation_error = _validate_tool_args(tool, tool_args)
                if validation_error:
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({"error": f"Invalid arguments for {tool_name}: {validation_error}"}),
                    })
                    continue

                # Confirmation check
                if tool.requires_confirmation:
                    # Use the first tool_call's id as the shared llm_message_id
                    # for all pending actions from this same LLM message.
                    if not pending_llm_message_id:
                        pending_llm_message_id = function_calls[0].get('call_id', '') or uuid_mod.uuid4().hex
                    # Store call_id so we can resume the loop after confirmation
                    args_with_call_id = {**tool_args, '_call_id': call_id}
                    action_log = AssistantActionLog.objects.create(
                        user_id=user_id,
                        conversation=conversation,
                        tool_name=tool_name,
                        tool_args=args_with_call_id,
                        result={},
                        success=False,
                        confirmed=False,
                        llm_message_id=pending_llm_message_id,
                    )
                    # Try to get rich confirmation data from the tool
                    try:
                        confirmation_data = tool.get_confirmation_data(tool_args, request)
                    except Exception:
                        confirmation_data = None
                    pending_actions.append({
                        'log_id': str(action_log.id),
                        'tool_name': tool_name,
                        'tool_args': tool_args,
                        'description': format_confirmation_text(tool_name, tool_args),
                        'confirmation_data': confirmation_data,
                    })
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({
                            "status": "pending_confirmation",
                            "message": f"Action '{tool_name}' requires user confirmation before execution.",
                            "action_id": str(action_log.id),
                        }),
                    })
                    has_pending = True
                    continue  # process remaining tool calls instead of break
                else:
                    # Execute immediately (read tools)
                    try:
                        result = tool.safe_execute(tool_args, request)

                        # Dynamic tool loading: when load_module_tools succeeds,
                        # add requested modules, rebuild tools, and persist to session.
                        if tool_name == 'load_module_tools' and isinstance(result, dict) and 'error' not in result:
                            for mid in result.get('loaded_for', tool_args.get('modules', [])):
                                loaded_modules.add(mid)
                            tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)
                            request.session['assistant_loaded_modules'] = list(loaded_modules)
                            if hasattr(request.session, 'modified'):
                                request.session.modified = True
                        # Unload: when unload_module_tools is called, remove from session
                        elif tool_name == 'unload_module_tools' and isinstance(result, dict) and 'error' not in result:
                            for mid in result.get('unloaded', tool_args.get('modules', [])):
                                loaded_modules.discard(mid)
                            tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)
                            request.session['assistant_loaded_modules'] = list(loaded_modules)
                            if hasattr(request.session, 'modified'):
                                request.session.modified = True

                        is_error = isinstance(result, dict) and 'error' in result and len(result) == 1
                        action_log = AssistantActionLog.objects.create(
                            user_id=user_id,
                            conversation=conversation,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            result=result,
                            success=not is_error,
                            confirmed=True,
                            error_message=result.get('error', '') if is_error else '',
                        )
                        # Feedback: tool_error
                        if is_error:
                            record_feedback(
                                event_type='tool_error',
                                user=user,
                                conversation=conversation,
                                action_log=action_log,
                                tool_name=tool_name,
                                user_message=original_message,
                                details={'error': result.get('error', ''), 'tool_args': tool_args},
                            )
                        # Feedback: zero_results
                        if (
                            tool_name == 'search_across_modules'
                            and isinstance(result, dict)
                            and result.get('total_results') == 0
                        ):
                            record_feedback(
                                event_type='zero_results',
                                user=user,
                                conversation=conversation,
                                action_log=action_log,
                                tool_name=tool_name,
                                user_message=original_message,
                                details={'query': result.get('query', tool_args.get('query', ''))},
                            )
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps(result),
                        })
                    except Exception as e:
                        logger.error(f"[ASSISTANT] Tool {tool_name} error: {e}", exc_info=True)
                        action_log = AssistantActionLog.objects.create(
                            user_id=user_id,
                            conversation=conversation,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            result={"error": str(e)},
                            success=False,
                            confirmed=True,
                            error_message=str(e),
                        )
                        # Feedback: tool_error (exception)
                        record_feedback(
                            event_type='tool_error',
                            user=user,
                            conversation=conversation,
                            action_log=action_log,
                            tool_name=tool_name,
                            user_message=original_message,
                            details={'error': str(e), 'tool_args': tool_args},
                        )
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": str(e)}),
                        })
            except Exception as e:
                # Safety net: guarantee every call_id gets a response
                logger.error(f"[ASSISTANT] Unexpected error processing tool {tool_name}: {e}", exc_info=True)
                tool_results.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': _json_dumps({"error": f"Internal error processing {tool_name}"}),
                })

        # If pending confirmations, stop the loop and return them to the frontend.
        # format_confirmation_text() already provides descriptions for each action.
        if has_pending:
            break

        # If we only have tool results (no pending), send them back for next iteration
        if tool_results:
            ai_input = tool_results
        else:
            break

    # Fallback: if AI returned no text and no pending actions, show a message
    if not response_text and not pending_actions:
        response_text = _("No se pudo generar una respuesta. Inténtalo de nuevo o reformula tu mensaje.")

    return {
        'response_text': response_text,
        'pending_actions': pending_actions,
        'conversation_id': conversation.id,
        'tier_info': tier_info,
    }


def execute_confirmed_action(action_log, request, plan_request_id=None):
    """
    Execute a confirmed action. Shared between HTMX and API views.

    Args:
        action_log: AssistantActionLog instance
        request: Django request (or fake request for background threads)
        plan_request_id: optional request_id to inject into execute_plan args
            so the tool can publish per-step progress to the polling cache.

    Returns:
        dict with keys: success, message, result
    """
    tool = get_tool(action_log.tool_name)
    if not tool:
        action_log.error_message = f"Tool {action_log.tool_name} not found"
        action_log.save()
        return {'success': False, 'message': f'Tool {action_log.tool_name} not found', 'result': {}}

    # Mark confirmed BEFORE executing — if server restarts mid-execution
    # (e.g. install_modules), the button won't reappear on page reload
    action_log.confirmed = True
    action_log.save(update_fields=['confirmed'])

    # Remove internal _call_id before passing args to the tool
    exec_args = {k: v for k, v in action_log.tool_args.items() if k != '_call_id'}

    # For execute_plan: inject the request_id so the tool can emit per-step
    # progress updates that the frontend polls via poll_progress.
    if action_log.tool_name == 'execute_plan' and plan_request_id:
        exec_args = {**exec_args, '_plan_request_id': plan_request_id}

    try:
        result = tool.safe_execute(exec_args, request)
        # safe_execute returns error dict instead of raising for common errors
        if isinstance(result, dict) and 'error' in result and len(result) == 1:
            action_log.result = result
            action_log.success = False
            action_log.error_message = result['error']
            action_log.save()
            return {'success': False, 'message': result['error'], 'result': result}
        # Check inner success field (e.g. execute_plan returns {success: false, errors: [...]})
        if isinstance(result, dict) and result.get('success') is False:
            errors = result.get('errors', [])
            if errors:
                error_msg = '; '.join(
                    e if isinstance(e, str) else str(e) for e in errors
                )
            else:
                error_msg = 'Action completed with errors'
            action_log.result = result
            action_log.success = False
            action_log.error_message = error_msg
            action_log.save()
            succeeded = result.get('succeeded', 0)
            total = result.get('total_steps', 0)
            msg = f'{succeeded}/{total} steps succeeded. Errors: {error_msg}' if total else error_msg
            return {'success': False, 'message': msg, 'result': result}
        action_log.result = result
        action_log.success = True
        action_log.save()
        return {'success': True, 'message': 'Action confirmed and executed successfully.', 'result': result}
    except Exception as e:
        logger.error(f"[ASSISTANT] Confirm action error: {e}", exc_info=True)
        action_log.result = {"error": str(e)}
        action_log.success = False
        action_log.error_message = str(e)
        action_log.save()
        return {'success': False, 'message': f'Error executing action: {str(e)}', 'result': {"error": str(e)}}


# ============================================================================
# HTMX CHAT VIEW
# ============================================================================

@login_required
@permission_required('assistant.use_chat')
@require_POST
def chat(request):
    """
    Process a chat message through the agentic loop (HTMX endpoint).
    Returns HTML partials.
    """
    message = request.POST.get('message', '').strip()
    conversation_id = request.POST.get('conversation_id', '')
    context = request.POST.get('context', 'general')
    uploaded_file = request.FILES.get('file')

    if not message and not uploaded_file:
        return HttpResponse(
            render_to_string('assistant/partials/message.html', {
                'role': 'assistant',
                'content': _('Escribe un mensaje.'),
            }, request=request),
        )

    user_id = request.session.get('local_user_id')

    # Get or create conversation
    conversation = _get_or_create_conversation(user_id, conversation_id, context)

    # Get user object
    from apps.accounts.models import LocalUser
    try:
        user = LocalUser.objects.get(id=user_id)
    except LocalUser.DoesNotExist:
        return _error_response("User not found", request)

    # Build input (text or multimodal)
    ai_input = message

    if uploaded_file:
        if uploaded_file.size > 10 * 1024 * 1024:
            return _error_response("File too large. Maximum size is 10 MB.", request)

        mime_type = uploaded_file.content_type or ''
        image_types = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')

        if mime_type in image_types:
            file_bytes = uploaded_file.read()
            b64 = base64.b64encode(file_bytes).decode('utf-8')
            ai_input = [
                {"type": "input_text", "text": message or "Describe this image."},
                {"type": "input_image", "image_url": f"data:{mime_type};base64,{b64}"},
            ]
        elif mime_type == 'application/pdf':
            ai_input = _process_pdf_upload(uploaded_file, message)
        else:
            return _error_response(
                "Unsupported file type. Please use JPEG, PNG, WebP, GIF, or PDF.",
                request,
            )

    # Track conversation memory
    _track_conversation_message(conversation, message)

    # Persist user message locally
    if message:
        AssistantMessage.save_message(conversation, 'user', message)

    # Generate a request_id for progress tracking
    request_id = uuid_mod.uuid4().hex[:16]

    # Create persistent AssistantRequest in DB (survives hub restart)
    db_request = AssistantRequest.objects.create(
        conversation=conversation,
        user=user,
        user_message=message,
        status='pending',
    )

    # Capture session data needed by the background thread
    session_data = dict(request.session)

    def _background_task():
        """Run the agentic loop in a background thread."""
        from django.test import RequestFactory
        # Create a minimal request object for the background thread
        fake_request = RequestFactory().get('/')
        fake_request.session = session_data

        try:
            result = run_agentic_loop(
                user, conversation, ai_input, context, fake_request,
                request_id=request_id,
                db_request_id=db_request.id,
            )
            # Store the completed result
            cache.set(f'assistant_result_{request_id}', result, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'complete', '', db_request_id=db_request.id)

            # Persist result to DB
            AssistantRequest.objects.filter(id=db_request.id).update(
                status='complete',
                response_text=result.get('response_text', ''),
                pending_actions=result.get('pending_actions', []),
            )

        except AgenticLoopError as e:
            # AgenticLoopError has user-facing messages (already sanitized)
            cache.set(f'assistant_result_{request_id}', {'error': str(e)}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', str(e), db_request_id=db_request.id)
        except Exception as e:
            logger.error(f"[ASSISTANT] Background error: {e}", exc_info=True)
            friendly_msg = _("Algo salió mal. Inténtalo de nuevo o abre una nueva conversación.")
            cache.set(f'assistant_result_{request_id}', {'error': friendly_msg}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', friendly_msg, db_request_id=db_request.id)

    # Set initial progress before starting thread to avoid race condition
    # where first poll fires before the thread sets any progress entry
    _set_progress(request_id, 'thinking', _('Analizando tu solicitud...'), db_request_id=db_request.id)

    # Start background thread
    thread = threading.Thread(target=_background_task, daemon=True)
    thread.start()

    # Return polling partial
    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': _('Analizando tu solicitud...'),
    }, request=request)

    response = HttpResponse(html)
    response['X-Conversation-Id'] = str(conversation.id)
    return response


@login_required
@permission_required('assistant.use_chat')
@require_POST
def chat_stream(request):
    """
    Full agentic SSE streaming endpoint — ChatGPT-like experience.

    Runs the complete agentic loop within a single SSE connection:
    1. Streams LLM text to the browser in real-time
    2. When the LLM emits tool calls, executes them server-side
    3. Sends tool results back to the LLM for another round
    4. Repeats until done or a confirmation is needed

    SSE event types:
      data: {"type": "text_delta", "text": "..."}
      data: {"type": "tool_start", "name": "...", "call_id": "..."}
      data: {"type": "tool_result", "name": "...", "call_id": "...", "success": true}
      data: {"type": "confirmation", "actions": [...]}
      data: {"type": "conv_id", "conversation_id": "..."}
      data: {"type": "tier_info", ...}
      data: {"type": "error", "message": "..."}
      data: [DONE]
    """
    import requests as http_requests
    import select
    from apps.configuration.models import HubConfig
    from apps.accounts.models import LocalUser
    from django.conf import settings

    message = request.POST.get('message', '').strip()
    conversation_id = request.POST.get('conversation_id', '')
    context = request.POST.get('context', 'general')

    if not message:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": _("Escribe un mensaje.")})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    user_id = request.session.get('local_user_id')
    conversation = _get_or_create_conversation(user_id, conversation_id, context)

    try:
        user = LocalUser.objects.get(id=user_id)
    except LocalUser.DoesNotExist:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": "User not found."})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    _track_conversation_message(conversation, message)
    if message:
        AssistantMessage.save_message(conversation, 'user', message)

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": "Hub is not connected to Cloud."})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')

    # Dynamic tool loading
    from assistant.tools import (
        _get_active_module_ids, VIRTUAL_MODULES,
        preload_modules_for_message, resolve_module_dependencies,
        is_read_only_tool,
    )
    active_module_ids = set(_get_active_module_ids())
    is_new_session = conversation.message_count == 1
    if is_new_session:
        request.session.pop('assistant_loaded_modules', None)
        loaded_modules = set()
    else:
        session_loaded = request.session.get('assistant_loaded_modules', [])
        loaded_modules = set(session_loaded) & active_module_ids

    if message:
        preload = preload_modules_for_message(message, active_module_ids, loaded_modules)
        if preload:
            real_preload = {m for m in preload if m not in VIRTUAL_MODULES}
            if real_preload:
                resolved, _ = resolve_module_dependencies(real_preload, active_module_ids)
                preload = preload | resolved
            loaded_modules |= preload
            request.session['assistant_loaded_modules'] = list(loaded_modules)

    user_role = request.session.get('user_role', 'employee')
    tools = get_tools_for_context(context, user, loaded_modules=loaded_modules,
                                  user_role=user_role)
    instructions = build_system_prompt(request, context, message=message,
                                       is_new_session=is_new_session)
    conversation_id_str = str(conversation.id)
    session_data = dict(request.session)

    def _stream_one_llm_call(payload):
        """
        Make one streaming call to Cloud and yield SSE events.
        Returns (response_output, accumulated_text, function_calls, tier_info, error).
        """
        accumulated_text = []
        function_calls = []
        response_output = None
        tier_info_data = None
        error = None

        try:
            with http_requests.post(
                f"{base_url}/api/hubs/me/assistant/chat/stream/",
                json=payload,
                headers={
                    'Authorization': f'Bearer {config.hub_jwt}',
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                },
                stream=True,
                timeout=(10, 300),
            ) as resp:
                if resp.status_code != 200:
                    try:
                        err_data = resp.json()
                        err_msg = err_data.get('error', f'Cloud error {resp.status_code}')
                    except Exception:
                        err_msg = f'Cloud error {resp.status_code}'
                    error = err_msg
                    return response_output, accumulated_text, function_calls, tier_info_data, error

                # Extract tier info from response headers
                tier_header = resp.headers.get('X-Assistant-Tier')
                usage_header = resp.headers.get('X-Assistant-Usage')
                if tier_header:
                    tier_info_data = {'tier': tier_header}
                    if usage_header:
                        try:
                            tier_info_data.update(json.loads(usage_header))
                        except (json.JSONDecodeError, TypeError):
                            pass

                resp.raw.decode_content = True
                buf = b''
                while True:
                    ready, _, _ = select.select([resp.raw], [], [], 15)
                    if ready:
                        chunk = resp.raw.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b'\n' in buf:
                            line_bytes, buf = buf.split(b'\n', 1)
                            line = line_bytes.decode('utf-8', errors='replace').rstrip('\r')
                            if not line or line.startswith(':'):
                                continue
                            if not line.startswith('data: '):
                                continue
                            raw = line[6:].strip()
                            if raw == '[DONE]':
                                continue
                            try:
                                evt = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                continue
                            evt_type = evt.get('type', '')
                            if evt_type == 'text_delta':
                                accumulated_text.append(evt.get('text', ''))
                                yield line + '\n\n'
                            elif evt_type == 'function_call':
                                function_calls.append(evt)
                                # Don't forward raw function_call — we'll emit tool_start instead
                            elif evt_type == 'response':
                                response_output = evt.get('output', [])
                            elif evt_type == 'error':
                                error = evt.get('message', 'Unknown error')
                                yield line + '\n\n'
                    else:
                        yield ': keepalive\n\n'

        except http_requests.exceptions.Timeout:
            error = _("La solicitud tardó demasiado. Inténtalo con un mensaje más corto.")
        except Exception as e:
            logger.error(f"[ASSISTANT STREAM] Proxy error: {e}", exc_info=True)
            error = _("Error de conexión. Inténtalo de nuevo.")

        return response_output, accumulated_text, function_calls, tier_info_data, error

    # We use a mutable container so the inner generator can update outer state
    state = {
        'loaded_modules': loaded_modules,
        'tools': tools,
    }

    def _agentic_stream():
        yield ': keepalive\n\n'
        yield f'data: {json.dumps({"type": "conv_id", "conversation_id": conversation_id_str})}\n\n'

        all_accumulated_text = []
        ai_input = message
        call_counts = {}
        tier_info = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            payload = {
                'input': ai_input,
                'instructions': instructions,
                'conversation_id': conversation_id_str,
                'tools': state['tools'],
            }
            if is_new_session and iteration == 0:
                payload['new_session'] = True

            # Stream one LLM call — yields text_delta events to browser
            response_output = None
            accumulated_text = []
            function_calls = []
            tier_info_data = None
            error = None

            for event_or_result in _stream_one_llm_call(payload):
                if isinstance(event_or_result, str):
                    yield event_or_result
                else:
                    # This shouldn't happen since _stream_one_llm_call yields strings
                    pass

            # Retrieve return values from the generator
            # We need to refactor — _stream_one_llm_call can't both yield AND return.
            # Instead, use shared state via a container.
            break  # placeholder — we need a different approach

        yield 'data: [DONE]\n\n'

    # The problem: Python generators can't both yield AND return values.
    # Solution: use a shared mutable container for the non-yielded outputs.

    def _agentic_stream_v2():
        nonlocal loaded_modules, tools

        yield ': keepalive\n\n'
        yield f'data: {json.dumps({"type": "conv_id", "conversation_id": conversation_id_str})}\n\n'

        all_accumulated_text = []
        ai_input = message
        call_counts = {}
        tier_info = None
        needs_restart = False

        for iteration in range(MAX_TOOL_ITERATIONS):
            payload = {
                'input': ai_input,
                'instructions': instructions,
                'conversation_id': conversation_id_str,
                'tools': tools,
            }
            if is_new_session and iteration == 0:
                payload['new_session'] = True

            # Stream one LLM call — collect events and forward text to browser
            accumulated_text = []
            function_calls = []
            response_output = None
            stream_tier_info = None
            stream_error = None

            try:
                with http_requests.post(
                    f"{base_url}/api/hubs/me/assistant/chat/stream/",
                    json=payload,
                    headers={
                        'Authorization': f'Bearer {config.hub_jwt}',
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                    },
                    stream=True,
                    timeout=(10, 300),
                ) as resp:
                    if resp.status_code != 200:
                        try:
                            err_data = resp.json()
                            err_msg = err_data.get('error', f'Cloud error {resp.status_code}')
                        except Exception:
                            err_msg = f'Cloud error {resp.status_code}'
                        yield f'data: {json.dumps({"type": "error", "message": err_msg})}\n\n'
                        yield 'data: [DONE]\n\n'
                        return

                    # Extract tier info from response headers
                    tier_header = resp.headers.get('X-Assistant-Tier')
                    usage_header = resp.headers.get('X-Assistant-Usage')
                    if tier_header:
                        stream_tier_info = {'tier': tier_header}
                        if usage_header:
                            try:
                                stream_tier_info.update(json.loads(usage_header))
                            except (json.JSONDecodeError, TypeError):
                                pass

                    resp.raw.decode_content = True
                    buf = b''
                    while True:
                        ready, _, _ = select.select([resp.raw], [], [], 15)
                        if ready:
                            chunk = resp.raw.read(4096)
                            if not chunk:
                                break
                            buf += chunk
                            while b'\n' in buf:
                                line_bytes, buf = buf.split(b'\n', 1)
                                line = line_bytes.decode('utf-8', errors='replace').rstrip('\r')
                                if not line or line.startswith(':'):
                                    continue
                                if not line.startswith('data: '):
                                    continue
                                raw = line[6:].strip()
                                if raw == '[DONE]':
                                    continue
                                try:
                                    evt = json.loads(raw)
                                except (json.JSONDecodeError, TypeError):
                                    continue
                                evt_type = evt.get('type', '')
                                if evt_type == 'text_delta':
                                    accumulated_text.append(evt.get('text', ''))
                                    yield line + '\n\n'
                                elif evt_type == 'function_call':
                                    function_calls.append(evt)
                                elif evt_type == 'response':
                                    response_output = evt.get('output', [])
                                elif evt_type == 'error':
                                    stream_error = evt.get('message', '')
                                    yield line + '\n\n'
                        else:
                            yield ': keepalive\n\n'

            except http_requests.exceptions.Timeout:
                yield f'data: {json.dumps({"type": "error", "message": _("La solicitud tardó demasiado. Inténtalo con un mensaje más corto.")})}\n\n'
                yield 'data: [DONE]\n\n'
                return
            except Exception as e:
                logger.error(f"[ASSISTANT STREAM] Proxy error: {e}", exc_info=True)
                yield f'data: {json.dumps({"type": "error", "message": _("Error de conexión. Inténtalo de nuevo.")})}\n\n'
                yield 'data: [DONE]\n\n'
                return

            if stream_error:
                yield 'data: [DONE]\n\n'
                return

            if stream_tier_info:
                tier_info = stream_tier_info
                yield f'data: {json.dumps({"type": "tier_info", **stream_tier_info})}\n\n'

            all_accumulated_text.extend(accumulated_text)

            # No function calls — we're done
            if not function_calls:
                break

            # Execute tools server-side and build tool_results for next LLM call
            tool_results = []
            pending_actions = []
            has_pending = False
            pending_llm_message_id = ''

            for fc in function_calls:
                tool_name = fc.get('name', '')
                call_id = fc.get('call_id', '')
                try:
                    tool_args = json.loads(fc.get('arguments', '{}'))
                except json.JSONDecodeError:
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({"error": f"Malformed JSON arguments for {tool_name}"}),
                    })
                    continue

                try:
                    tool = get_tool(tool_name)
                    if not tool:
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": f"Unknown tool: {tool_name}"}),
                        })
                        continue

                    # Permission check
                    if tool.required_permission and not user.has_perm(tool.required_permission):
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": f"Permission denied: {tool.required_permission}"}),
                        })
                        continue

                    # Employee read-only guard
                    if user_role == 'employee' and not is_read_only_tool(tool_name):
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": "Read-only access."}),
                        })
                        continue

                    # Anti-loop detection
                    ch = _call_hash(tool_name, tool_args)
                    call_counts[ch] = call_counts.get(ch, 0) + 1
                    if call_counts[ch] > MAX_IDENTICAL_CALLS:
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": f"Already called {tool_name} with same args."}),
                        })
                        continue

                    # Schema validation
                    validation_error = _validate_tool_args(tool, tool_args)
                    if validation_error:
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({"error": f"Invalid arguments: {validation_error}"}),
                        })
                        continue

                    # Confirmation check — stop loop, send to frontend
                    if tool.requires_confirmation:
                        if not pending_llm_message_id:
                            pending_llm_message_id = function_calls[0].get('call_id', '') or uuid_mod.uuid4().hex
                        args_with_call_id = {**tool_args, '_call_id': call_id}
                        action_log = AssistantActionLog.objects.create(
                            user_id=str(user.id),
                            conversation=conversation,
                            tool_name=tool_name,
                            tool_args=args_with_call_id,
                            result={},
                            success=False,
                            confirmed=False,
                            llm_message_id=pending_llm_message_id,
                        )
                        try:
                            confirmation_data = tool.get_confirmation_data(tool_args, request)
                        except Exception:
                            confirmation_data = None
                        pending_actions.append({
                            'log_id': str(action_log.id),
                            'tool_name': tool_name,
                            'tool_args': tool_args,
                            'description': format_confirmation_text(tool_name, tool_args),
                            'confirmation_data': confirmation_data,
                        })
                        tool_results.append({
                            'type': 'function_call_output',
                            'call_id': call_id,
                            'output': _json_dumps({
                                "status": "pending_confirmation",
                                "message": f"Action '{tool_name}' requires user confirmation.",
                                "action_id": str(action_log.id),
                            }),
                        })
                        has_pending = True
                        continue

                    # Execute tool
                    yield f'data: {json.dumps({"type": "tool_start", "name": tool_name, "call_id": call_id})}\n\n'

                    result = tool.safe_execute(tool_args, request)

                    # Dynamic tool loading
                    if tool_name == 'load_module_tools' and isinstance(result, dict) and 'error' not in result:
                        for mid in result.get('loaded_for', tool_args.get('modules', [])):
                            loaded_modules.add(mid)
                        tools = get_tools_for_context(context, user, loaded_modules=loaded_modules,
                                                      user_role=user_role)
                        request.session['assistant_loaded_modules'] = list(loaded_modules)
                    elif tool_name == 'unload_module_tools' and isinstance(result, dict) and 'error' not in result:
                        for mid in result.get('unloaded', tool_args.get('modules', [])):
                            loaded_modules.discard(mid)
                        tools = get_tools_for_context(context, user, loaded_modules=loaded_modules,
                                                      user_role=user_role)
                        request.session['assistant_loaded_modules'] = list(loaded_modules)

                    is_error = isinstance(result, dict) and 'error' in result and len(result) == 1
                    AssistantActionLog.objects.create(
                        user_id=str(user.id),
                        conversation=conversation,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        result=result,
                        success=not is_error,
                        confirmed=True,
                        error_message=result.get('error', '') if is_error else '',
                    )

                    yield f'data: {json.dumps({"type": "tool_result", "name": tool_name, "call_id": call_id, "success": not is_error})}\n\n'

                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps(result),
                    })

                except Exception as e:
                    logger.error(f"[ASSISTANT STREAM] Tool {tool_name} error: {e}", exc_info=True)
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps({"error": str(e)}),
                    })
                    yield f'data: {json.dumps({"type": "tool_result", "name": tool_name, "call_id": call_id, "success": False})}\n\n'

            # If confirmations pending, render HTML and send via SSE
            if has_pending:
                from django.test import RequestFactory
                fake_req = RequestFactory().get('/')
                fake_req.session = session_data
                confirmation_html_parts = []
                for action in pending_actions:
                    confirmation_html_parts.append(
                        render_to_string('assistant/partials/confirmation.html', {
                            'log_id': action['log_id'],
                            'tool_name': action['tool_name'],
                            'tool_args': action['tool_args'],
                            'description': action['description'],
                            'confirmation_data': action.get('confirmation_data'),
                        }, request=fake_req)
                    )
                yield f'data: {json.dumps({"type": "confirmation", "html": "".join(confirmation_html_parts)})}\n\n'
                break

            # Feed tool results back to LLM for next iteration
            if tool_results:
                ai_input = tool_results
            else:
                break

        # Persist assistant response
        full_text = ''.join(all_accumulated_text)
        if full_text:
            try:
                AssistantMessage.save_message(conversation, 'assistant', full_text)
            except Exception as e:
                logger.warning(f"[ASSISTANT STREAM] Failed to persist message: {e}")

        yield 'data: [DONE]\n\n'

    response = StreamingHttpResponse(_agentic_stream_v2(), content_type='text/event-stream')
    response['X-Conversation-Id'] = conversation_id_str
    response['X-Accel-Buffering'] = 'no'
    response['Cache-Control'] = 'no-cache'
    return response


@login_required
@permission_required('assistant.use_chat')
def poll_progress(request, request_id):
    """
    Poll for progress updates on a background chat request.
    Returns progress partial (continues polling) or final response (stops polling).
    """
    progress = cache.get(f'assistant_progress_{request_id}')
    if not progress:
        # No progress in cache — check if result exists (completed while we weren't polling)
        result = cache.get(f'assistant_result_{request_id}')
        if result:
            progress = {'type': 'complete', 'data': ''}
        else:
            # Neither progress nor result — likely hub restarted (LocMemCache wiped).
            # Fall back to DB for persistent request status.
            db_req = _check_db_request_status(request)
            if db_req:
                if db_req.status == 'complete':
                    # Reconstruct result from DB
                    cache.set(f'assistant_result_{request_id}', {
                        'response_text': db_req.response_text,
                        'pending_actions': db_req.pending_actions or [],
                        'conversation_id': db_req.conversation_id,
                    }, timeout=PROGRESS_CACHE_TIMEOUT)
                    progress = {'type': 'complete', 'data': ''}
                elif db_req.status == 'error':
                    cache.set(f'assistant_result_{request_id}', {
                        'error': db_req.error_message or 'Unknown error',
                    }, timeout=PROGRESS_CACHE_TIMEOUT)
                    progress = {'type': 'error', 'data': db_req.error_message}
                elif db_req.status in ('pending', 'processing'):
                    # Still in progress — show last known progress
                    return HttpResponse(render_to_string('assistant/partials/progress.html', {
                        'request_id': request_id,
                        'message': db_req.progress_message or _('Procesando...'),
                    }, request=request))

            if not progress:
                # Check the last confirmed action to give a meaningful message.
                user_id = request.session.get('local_user_id')
                message = _get_post_restart_message(user_id)
                return HttpResponse(render_to_string('assistant/partials/message.html', {
                    'role': 'system',
                    'content': message,
                }, request=request))

    if progress['type'] in ('complete', 'error'):
        # Get the final result
        result = cache.get(f'assistant_result_{request_id}')

        # Clean up cache
        cache.delete(f'assistant_progress_{request_id}')
        cache.delete(f'assistant_result_{request_id}')

        if not result or 'error' in result:
            error_msg = (result or {}).get('error', 'Unknown error')
            return HttpResponse(render_to_string('assistant/partials/message.html', {
                'role': 'system',
                'content': error_msg,
                'error': True,
            }, request=request))

        # Persist assistant response locally
        if result.get('response_text') and result.get('conversation_id'):
            try:
                conv = AssistantConversation.objects.get(id=result['conversation_id'])
                AssistantMessage.save_message(conv, 'assistant', result['response_text'])
            except AssistantConversation.DoesNotExist:
                pass

        # Build final response HTML
        html_parts = []

        if result.get('response_text'):
            import markdown as md
            rendered_content = md.markdown(
                result['response_text'],
                extensions=['tables', 'fenced_code', 'nl2br'],
            )
            html_parts.append(render_to_string('assistant/partials/message.html', {
                'role': 'assistant',
                'content': rendered_content,
            }, request=request))

        for action in result.get('pending_actions', []):
            html_parts.append(render_to_string('assistant/partials/confirmation.html', {
                'log_id': action['log_id'],
                'tool_name': action['tool_name'],
                'tool_args': action['tool_args'],
                'description': action['description'],
                'confirmation_data': action.get('confirmation_data'),
            }, request=request))

        resp = HttpResponse(''.join(html_parts))
        if result.get('tier_info'):
            resp['X-Assistant-Tier'] = result['tier_info'].get('tier', '')
            resp['X-Assistant-Usage'] = _json_dumps({
                'sessions_used': result['tier_info'].get('sessions_used', 0),
                'sessions_limit': result['tier_info'].get('sessions_limit', 0),
                'tier_name': result['tier_info'].get('tier_name', ''),
            })
        return resp
    else:
        # Still processing — return progress partial that continues polling
        return HttpResponse(render_to_string('assistant/partials/progress.html', {
            'request_id': request_id,
            'message': progress.get('data', _('Procesando...')),
        }, request=request))


# ============================================================================
# HTMX CONFIRMATION ACTIONS
# ============================================================================

@login_required
@permission_required('assistant.use_chat')
@require_POST
def confirm_action(request, log_id):
    """Confirm and execute a pending write action (HTMX endpoint)."""
    user_id = request.session.get('local_user_id')

    try:
        action_log = AssistantActionLog.objects.get(
            id=log_id,
            user_id=user_id,
            confirmed=False,
        )
    except AssistantActionLog.DoesNotExist:
        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': _('Acción no encontrada o ya procesada.'),
        }, request=request))

    # execute_plan runs asynchronously so per-step progress is streamed
    # to the frontend via the standard poll_progress mechanism.
    if action_log.tool_name == 'execute_plan':
        return _confirm_execute_plan_async(request, action_log, user_id)

    result = execute_confirmed_action(action_log, request)

    if result['success']:
        return _resume_loop_after_confirm(request, action_log, result, user_id)
    else:
        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': result['message'],
            'error': True,
        }, request=request))


def _confirm_execute_plan_async(request, action_log, user_id):
    """
    Run execute_plan in a background thread, streaming per-step progress via
    the standard poll_progress cache mechanism.

    Returns a progress partial immediately. When the plan finishes, the
    agentic loop is resumed (same as a normal confirm success flow).
    """
    request_id = uuid_mod.uuid4().hex[:16]
    session_data = dict(request.session)
    conversation = action_log.conversation
    context = (conversation.context or 'general') if conversation else 'general'

    from apps.accounts.models import LocalUser
    try:
        user = LocalUser.objects.get(id=user_id)
    except LocalUser.DoesNotExist:
        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': 'User not found.',
            'error': True,
        }, request=request))

    _set_progress(request_id, 'tool', _('Ejecutando plan...'))

    def _run_plan():
        from django.test import RequestFactory
        fake_request = RequestFactory().get('/')
        fake_request.session = session_data
        needs_restart = False
        try:
            plan_result = execute_confirmed_action(
                action_log, fake_request, plan_request_id=request_id,
            )
            # Check if blueprint install flagged a deferred restart.
            # ExecutePlan returns {results: [{result: {result: {restart_scheduled: True}}}]}
            plan_inner = plan_result.get('result', {})
            if isinstance(plan_inner, dict):
                for step_res in plan_inner.get('results', []):
                    r = step_res.get('result', {})
                    if isinstance(r, dict):
                        # _install_blueprint wraps BlueprintService result in 'result' key
                        inner = r.get('result', r)
                        if isinstance(inner, dict) and inner.get('restart_scheduled'):
                            needs_restart = True
                            break

            if plan_result['success']:
                # Build resume_input and continue the agentic loop
                call_id = action_log.tool_args.get('_call_id', '')
                if call_id:
                    resume_input = [{
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps(plan_inner),
                    }]
                else:
                    resume_input = (
                        f"[execute_plan completed. "
                        f"Result: {_json_dumps(plan_inner)}] "
                        f"Continue with the next steps."
                    )
                try:
                    loop_result = run_agentic_loop(
                        user, conversation, resume_input, context, fake_request,
                        request_id=request_id,
                    )
                    cache.set(f'assistant_result_{request_id}', loop_result, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'complete', '')

                    # Now that result is safely stored, schedule the deferred restart
                    if needs_restart:
                        from apps.core.utils import schedule_server_restart
                        schedule_server_restart(delay=5)
                except AgenticLoopError as e:
                    cache.set(f'assistant_result_{request_id}', {'error': str(e)}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', str(e))
                    if needs_restart:
                        from apps.core.utils import schedule_server_restart
                        schedule_server_restart(delay=5)
                except Exception as e:
                    logger.error(f"[ASSISTANT] Plan resume loop error: {e}", exc_info=True)
                    cache.set(f'assistant_result_{request_id}', {'error': _('Algo salió mal.')}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', _('Algo salió mal.'))
                    if needs_restart:
                        from apps.core.utils import schedule_server_restart
                        schedule_server_restart(delay=5)
            else:
                # Plan failed — build an error message with rollback info
                err_msg = plan_result.get('message', 'Plan execution failed.')
                rolled_back = plan_inner.get('rolled_back', [])
                if rolled_back:
                    rb_names = [
                        r.get('description', r.get('action', ''))
                        for r in rolled_back if r.get('rolled_back')
                    ]
                    if rb_names:
                        err_msg += f" Rolled back: {', '.join(rb_names)}."
                cache.set(
                    f'assistant_result_{request_id}',
                    {'error': err_msg},
                    timeout=PROGRESS_CACHE_TIMEOUT,
                )
                _set_progress(request_id, 'error', err_msg)
                if needs_restart:
                    from apps.core.utils import schedule_server_restart
                    schedule_server_restart(delay=5)
        except Exception as e:
            logger.error(f"[ASSISTANT] execute_plan async error: {e}", exc_info=True)
            cache.set(f'assistant_result_{request_id}', {'error': _('Algo salió mal.')}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', _('Algo salió mal.'))
            if needs_restart:
                from apps.core.utils import schedule_server_restart
                schedule_server_restart(delay=5)

    thread = threading.Thread(target=_run_plan, daemon=True)
    thread.start()

    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': _('Ejecutando plan...'),
    }, request=request)
    return HttpResponse(html)


def _resume_loop_after_confirm(request, action_log, result, user_id):
    """
    After a non-plan confirmed action succeeds, resume the agentic loop
    in a background thread and return a progress partial.
    """
    conversation = action_log.conversation
    context = conversation.context or 'general'

    from apps.accounts.models import LocalUser
    user = LocalUser.objects.get(id=user_id)

    # Check if this action belongs to a group (same llm_message_id).
    # If sibling actions are still unconfirmed, don't resume the loop yet.
    llm_message_id = action_log.llm_message_id
    if llm_message_id:
        sibling_pending = AssistantActionLog.objects.filter(
            llm_message_id=llm_message_id,
            confirmed=False,
        ).exclude(id=action_log.id)
        if sibling_pending.exists():
            # More confirmations needed — tell the user and wait
            remaining = sibling_pending.count()
            noun = 'action' if remaining == 1 else 'actions'
            return HttpResponse(render_to_string('assistant/partials/message.html', {
                'role': 'system',
                'content': f'Action confirmed. Please confirm the remaining {remaining} {noun} above to continue.',
            }, request=request))

        # All siblings confirmed — collect all their results in order
        all_logs = AssistantActionLog.objects.filter(
            llm_message_id=llm_message_id,
        ).order_by('created_at')
        resume_input = []
        for log in all_logs:
            cid = log.tool_args.get('_call_id', '')
            if cid:
                resume_input.append({
                    'type': 'function_call_output',
                    'call_id': cid,
                    'output': _json_dumps(log.result),
                })
        if not resume_input:
            # Fallback: use just the current action's result
            resume_input = None
    else:
        resume_input = None

    # Build resume_input for single (ungrouped) actions
    if resume_input is None:
        call_id = action_log.tool_args.get('_call_id', '')
        if call_id:
            resume_input = [{
                'type': 'function_call_output',
                'call_id': call_id,
                'output': _json_dumps(result.get('result', {})),
            }]
        else:
            # Fallback: no call_id stored (old action logs)
            resume_input = (
                f"[Tool '{action_log.tool_name}' executed successfully. "
                f"Result: {_json_dumps(result.get('result', {}))}] "
                f"Continue with the next steps."
            )

    request_id = uuid_mod.uuid4().hex[:16]
    session_data = dict(request.session)

    def _resume_loop():
        from django.test import RequestFactory
        fake_request = RequestFactory().get('/')
        fake_request.session = session_data
        try:
            loop_result = run_agentic_loop(
                user, conversation, resume_input, context, fake_request,
                request_id=request_id,
            )
            cache.set(f'assistant_result_{request_id}', loop_result, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'complete', '')
        except AgenticLoopError as e:
            cache.set(f'assistant_result_{request_id}', {'error': str(e)}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', str(e))
        except Exception as e:
            logger.error(f"[ASSISTANT] Resume loop error: {e}", exc_info=True)
            cache.set(f'assistant_result_{request_id}', {'error': _('Algo salió mal.')}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', _('Algo salió mal.'))

    # Set initial progress before thread to avoid poll race condition
    _set_progress(request_id, 'thinking', _('Continuando configuración...'))

    thread = threading.Thread(target=_resume_loop, daemon=True)
    thread.start()

    # Return a polling partial so the frontend waits for the resumed loop
    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': _('Continuando configuración...'),
    }, request=request)
    return HttpResponse(html)


@login_required
@permission_required('assistant.use_chat')
@require_POST
def cancel_action(request, log_id):
    """Cancel a pending write action (HTMX endpoint)."""
    user_id = request.session.get('local_user_id')

    try:
        action_log = AssistantActionLog.objects.get(
            id=log_id,
            user_id=user_id,
            confirmed=False,
        )
    except AssistantActionLog.DoesNotExist:
        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': 'Action cancelled.',
        }, request=request))

    llm_message_id = action_log.llm_message_id

    if llm_message_id:
        # Cancel the whole group: collect all sibling logs (confirmed or not),
        # then resume the loop with cancellation results for all of them so
        # the LLM doesn't receive an incomplete tool-call sequence.
        all_logs = list(AssistantActionLog.objects.filter(
            llm_message_id=llm_message_id,
        ).order_by('created_at'))

        conversation = action_log.conversation
        context = (conversation.context or 'general') if conversation else 'general'

        from apps.accounts.models import LocalUser
        try:
            user = LocalUser.objects.get(id=user_id)
        except LocalUser.DoesNotExist:
            for log in all_logs:
                log.delete()
            return HttpResponse(render_to_string('assistant/partials/message.html', {
                'role': 'system',
                'content': 'Action cancelled.',
            }, request=request))

        # Build cancellation results for every log in the group
        resume_input = []
        for log in all_logs:
            cid = log.tool_args.get('_call_id', '')
            if cid:
                resume_input.append({
                    'type': 'function_call_output',
                    'call_id': cid,
                    'output': _json_dumps({'error': 'Action cancelled by user.'}),
                })
            log.delete()

        if resume_input and conversation:
            request_id = uuid_mod.uuid4().hex[:16]
            session_data = dict(request.session)

            def _resume_loop():
                from django.test import RequestFactory
                fake_request = RequestFactory().get('/')
                fake_request.session = session_data
                try:
                    loop_result = run_agentic_loop(
                        user, conversation, resume_input, context, fake_request,
                        request_id=request_id,
                    )
                    cache.set(f'assistant_result_{request_id}', loop_result, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'complete', '')
                except AgenticLoopError as e:
                    cache.set(f'assistant_result_{request_id}', {'error': str(e)}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', str(e))
                except Exception as e:
                    logger.error(f"[ASSISTANT] Cancel resume loop error: {e}", exc_info=True)
                    cache.set(f'assistant_result_{request_id}', {'error': _('Algo salió mal.')}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', _('Algo salió mal.'))

            thread = threading.Thread(target=_resume_loop, daemon=True)
            thread.start()

            html = render_to_string('assistant/partials/progress.html', {
                'request_id': request_id,
                'message': 'Notifying assistant of cancellation...',
            }, request=request)
            return HttpResponse(html)
    else:
        action_log.delete()

    return HttpResponse(render_to_string('assistant/partials/message.html', {
        'role': 'system',
        'content': 'Action cancelled.',
    }, request=request))


# ============================================================================
# HELPERS
# ============================================================================

def _get_or_create_conversation(user_id, conversation_id, context):
    """Get existing conversation or create a new one."""
    if conversation_id:
        try:
            return AssistantConversation.objects.get(
                id=conversation_id,
                user_id=user_id,
            )
        except (AssistantConversation.DoesNotExist, ValueError):
            pass

    # Before creating a new conversation, summarize the previous one
    _summarize_last_conversation(user_id)

    return AssistantConversation.objects.create(
        user_id=user_id,
        context=context,
    )


def _track_conversation_message(conversation, message):
    """Track message count and first message for conversation memory."""
    update_fields = ['message_count', 'updated_at']
    conversation.message_count += 1

    if conversation.message_count == 1 and message:
        conversation.first_message = message[:500]
        conversation.title = message[:100]
        update_fields.extend(['first_message', 'title'])

    conversation.save(update_fields=update_fields)


def _summarize_last_conversation(user_id):
    """Auto-summarize the most recent conversation from its action logs."""
    last = AssistantConversation.objects.filter(
        user_id=user_id,
        summary='',
    ).order_by('-updated_at').first()

    if not last:
        return

    skip_tools = {
        'get_hub_config', 'get_store_config', 'list_modules',
        'list_available_blocks', 'get_selected_blocks', 'list_roles',
        'list_tax_classes', 'list_employees', 'get_module_catalog',
        'list_products', 'list_services', 'list_customers',
        'list_categories', 'list_service_categories',
        'list_payment_methods', 'search_across_modules',
    }

    logs = AssistantActionLog.objects.filter(
        conversation=last, success=True,
    ).order_by('created_at')

    actions = []
    for log in logs:
        if log.tool_name in skip_tools:
            continue
        key_arg = ''
        for k in ('name', 'title', 'business_name', 'module_id', 'query'):
            val = log.tool_args.get(k)
            if val:
                key_arg = f" '{val}'"
                break
        actions.append(f"{log.tool_name}{key_arg}")

    if actions:
        last.summary = ', '.join(actions[:10])
        last.save(update_fields=['summary'])


def _call_cloud_proxy(request, input_data, instructions, tools,
                      conversation_id='', new_session=False):
    """
    Call the Cloud proxy endpoint to forward to Gemini.

    Uses the Hub's JWT token for authentication.
    Cloud determines the model based on the Hub's tier and manages
    conversation history server-side.

    Returns (response_data, tier_info) where tier_info is a dict with
    tier/usage data from response headers (or None).
    """
    import requests as http_requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from apps.configuration.models import HubConfig

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        raise CloudProxyError(
            "Hub is not connected to Cloud. Please configure Cloud connection first."
        )

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')

    payload = {
        'input': input_data,
        'instructions': instructions,
        'conversation_id': conversation_id,
    }

    if tools:
        payload['tools'] = tools

    if new_session:
        payload['new_session'] = True

    # Use session with retry for resilience against transient failures
    session = http_requests.Session()
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retry))

    response = session.post(
        f"{base_url}/api/hubs/me/assistant/chat/",
        json=payload,
        headers={
            'Authorization': f'Bearer {config.hub_jwt}',
            'Content-Type': 'application/json',
        },
        timeout=300,  # LLM responses can take time (agentic loops need more)
    )

    if response.status_code != 200:
        error_data = {}
        if response.headers.get('content-type', '').startswith('application/json'):
            error_data = response.json()

        raise CloudProxyError(
            error_data.get('error', response.text[:200]),
            status_code=response.status_code,
            error_data=error_data,
        )

    # Extract tier/usage info from response headers
    tier_info = None
    tier_header = response.headers.get('X-Assistant-Tier')
    usage_header = response.headers.get('X-Assistant-Usage')
    if tier_header:
        tier_info = {'tier': tier_header}
        if usage_header:
            try:
                tier_info.update(json.loads(usage_header))
            except (json.JSONDecodeError, TypeError):
                pass

    return response.json(), tier_info


def _error_response(message, request):
    """Return an error message as HTML partial."""
    return HttpResponse(render_to_string('assistant/partials/message.html', {
        'role': 'system',
        'content': message,
        'error': True,
    }, request=request))


# ============================================================================
# ASYNC CLOUD PROXY (SQS + Lambda)
# ============================================================================

ASYNC_POLL_INTERVAL = 1.0   # seconds between polls
ASYNC_POLL_MAX_WAIT = 600   # 10 minutes max wait (blueprint install can take 3-5 min per LLM call)


def _call_cloud_async_with_poll(request, input_data, instructions, tools,
                                conversation_id='', new_session=False,
                                request_id=None, db_request_id=None):
    """
    Send request to Cloud async endpoint, then poll until complete.

    Returns (response_data, tier_info) — same signature as _call_cloud_proxy.
    """
    import time

    cloud_request_id, tier_info = _call_cloud_proxy_async(
        request=request,
        input_data=input_data,
        instructions=instructions,
        tools=tools,
        conversation_id=conversation_id,
        new_session=new_session,
    )

    if not cloud_request_id:
        raise CloudProxyError("No request_id returned from async endpoint")

    if db_request_id:
        try:
            AssistantRequest.objects.filter(id=db_request_id).update(
                cloud_request_id=cloud_request_id,
            )
        except Exception:
            pass

    _set_progress(request_id, 'thinking', _('Procesando...'),
                  db_request_id=db_request_id)

    elapsed = 0
    while elapsed < ASYNC_POLL_MAX_WAIT:
        time.sleep(ASYNC_POLL_INTERVAL)
        elapsed += ASYNC_POLL_INTERVAL

        result = _poll_cloud_async_status(cloud_request_id)
        status = result.get('status', '')

        if status == 'complete':
            response_data = result.get('response', {})
            usage = result.get('usage')
            if usage and tier_info:
                tier_info.update(usage)
            return response_data, tier_info

        if status == 'error':
            error_msg = result.get('error_message', 'Unknown error from AI service')
            raise AgenticLoopError(error_msg)

        if status == 'processing':
            _set_progress(request_id, 'thinking', _('Pensando...'),
                          db_request_id=db_request_id)

    raise AgenticLoopError("AI request timed out. Please try again.")


def _call_cloud_proxy_async(request, input_data, instructions, tools,
                            conversation_id='', new_session=False):
    """
    Send chat request to the Cloud async endpoint.

    Returns (request_id, tier_info) — the Hub then polls for the result.
    """
    import requests as http_requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from apps.configuration.models import HubConfig

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        raise CloudProxyError(
            "Hub is not connected to Cloud. Please configure Cloud connection first."
        )

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')

    payload = {
        'input': input_data,
        'instructions': instructions,
        'conversation_id': conversation_id,
    }
    if tools:
        payload['tools'] = tools
    if new_session:
        payload['new_session'] = True

    session = http_requests.Session()
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retry))

    response = session.post(
        f"{base_url}/api/hubs/me/assistant/chat/async/",
        json=payload,
        headers={
            'Authorization': f'Bearer {config.hub_jwt}',
            'Content-Type': 'application/json',
        },
        timeout=30,  # Async endpoint should respond quickly
    )

    if response.status_code != 200:
        error_data = {}
        if response.headers.get('content-type', '').startswith('application/json'):
            error_data = response.json()
        raise CloudProxyError(
            error_data.get('error', response.text[:200]),
            status_code=response.status_code,
            error_data=error_data,
        )

    data = response.json()
    tier_info = None
    tier_header = response.headers.get('X-Assistant-Tier')
    usage_header = response.headers.get('X-Assistant-Usage')
    if tier_header:
        tier_info = {'tier': tier_header}
        if usage_header:
            try:
                tier_info.update(json.loads(usage_header))
            except (json.JSONDecodeError, TypeError):
                pass

    return data.get('request_id'), tier_info


def _poll_cloud_async_status(cloud_request_id):
    """
    Poll Cloud for the status of an async request.

    Returns dict: {status, response?, usage?, error_message?}
    """
    import requests as http_requests
    from apps.configuration.models import HubConfig

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        return {'status': 'error', 'error_message': 'Hub not connected to Cloud'}

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')

    try:
        resp = http_requests.get(
            f"{base_url}/api/hubs/me/assistant/chat/{cloud_request_id}/status/",
            headers={'Authorization': f'Bearer {config.hub_jwt}'},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        return {'status': 'error', 'error_message': f'Cloud returned {resp.status_code}'}
    except Exception as e:
        return {'status': 'error', 'error_message': str(e)}


def _is_async_available():
    """Check if Cloud supports async assistant (feature flag)."""
    # Async flow used as fallback when SSE streaming fails or for file uploads.
    # Primary flow is SSE streaming (direct Hub → Cloud → GPT-5).
    return True


def _process_pdf_upload(uploaded_file, message):
    """
    Process a PDF upload into multimodal input for the AI assistant.

    Tries PyMuPDF (fitz) to render pages as images.
    Falls back to text extraction if PyMuPDF is not installed.
    """
    file_bytes = uploaded_file.read()
    text_prompt = message or "Analyze this document."

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        input_parts = [{"type": "input_text", "text": text_prompt}]

        # Render up to 10 pages as images
        for page_num in range(min(len(doc), 10)):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode('utf-8')
            input_parts.append({
                "type": "input_image",
                "image_url": f"data:image/png;base64,{b64}",
            })

        doc.close()
        return input_parts

    except ImportError:
        logger.info("[ASSISTANT] PyMuPDF not installed, falling back to text hint")
        return (
            f"{text_prompt}\n\n[A PDF file was attached but could not be "
            f"processed as images. Please install PyMuPDF (`pip install PyMuPDF`) "
            f"for full PDF support, or upload an image/photo instead.]"
        )

    except Exception as e:
        logger.error(f"[ASSISTANT] PDF processing error: {e}", exc_info=True)
        return (
            f"{text_prompt}\n\n[Error processing PDF: {str(e)}. "
            f"Please try uploading an image instead.]"
        )


def _summarize_plan_steps(steps):
    """Build a compact human-readable summary of execute_plan steps."""
    from collections import Counter
    counts = Counter()
    parts = []
    seen_named = []

    for step in steps:
        action = step.get('action', '')
        params = step.get('params', {})

        if action == 'set_business_info':
            name = params.get('business_name', '')
            seen_named.append(_("Configurar negocio: %(name)s") % {'name': name} if name else _("Configurar datos del negocio"))
        elif action == 'set_tax_config':
            rate = params.get('tax_rate', '')
            seen_named.append(_("Configurar impuesto al %(rate)s%%") % {'rate': rate} if rate else _("Configurar impuestos"))
        elif action == 'set_regional_config':
            seen_named.append(_("Configurar región y formato"))
        elif action == 'install_blueprint':
            type_codes = params.get('type_codes', [])
            label = ', '.join(type_codes) if type_codes else _("plantilla")
            seen_named.append(_("Instalar plantilla (%(label)s)") % {'label': label})
        elif action == 'install_blueprint_products':
            bt = params.get('business_type', '')
            seen_named.append(_("Importar productos (%(type)s)") % {'type': bt} if bt else _("Importar productos"))
        elif action == 'complete_setup':
            seen_named.append(_("Completar configuración"))
        elif action in ('create_role', 'create_employee', 'create_tax_class',
                        'create_category', 'create_product', 'create_service',
                        'create_service_category', 'create_payment_method',
                        'create_zone', 'create_table', 'create_station',
                        'set_business_hours',
                        'bulk_create_zones', 'bulk_create_tables',
                        'update_store_config'):
            counts[action] += 1
        else:
            counts[action] += 1

    # Build count labels
    label_map = {
        'create_role': _('rol'), 'create_employee': _('empleado'),
        'create_tax_class': _('clase de impuesto'), 'create_category': _('categoría'),
        'create_product': _('producto'), 'create_service': _('servicio'),
        'create_service_category': _('categoría de servicio'),
        'create_payment_method': _('método de pago'),
        'create_zone': _('zona'), 'create_table': _('mesa'),
        'create_station': _('estación'), 'set_business_hours': _('horario'),
        'bulk_create_zones': _('lote de zonas'), 'bulk_create_tables': _('lote de mesas'),
        'update_store_config': _('actualizar tienda'),
    }
    for action, count in counts.items():
        label = label_map.get(action, action.replace('_', ' '))
        if count == 1:
            parts.append(str(label))
        else:
            parts.append(f"{count} {label}s")

    all_parts = seen_named + parts
    total = len(steps)
    summary = ', '.join(all_parts) if all_parts else _("%(total)s pasos") % {'total': total}
    return _("Plan (%(total)s pasos): %(summary)s") % {'total': total, 'summary': summary}


def format_confirmation_text(tool_name, tool_args):
    """Format a human-readable description of the pending action."""
    descriptions = {
        # Hub core tools
        'update_store_config': lambda a: _("Actualizar configuración de la tienda: %(fields)s") % {'fields': ', '.join(k for k, v in a.items() if v is not None)},
        'select_blocks': lambda a: _("Seleccionar bloques del dashboard: %(blocks)s") % {'blocks': ', '.join(a.get('block_slugs', []))},
        'enable_module': lambda a: _("Activar módulo: %(module)s") % {'module': a.get('module_id', '')},
        'disable_module': lambda a: _("Desactivar módulo: %(module)s") % {'module': a.get('module_id', '')},
        'create_role': lambda a: _("Crear rol: %(name)s") % {'name': a.get('display_name', a.get('name', ''))},
        'create_employee': lambda a: _("Crear empleado: %(name)s (%(role)s)") % {'name': a.get('name', ''), 'role': a.get('role_name', '')},
        'create_tax_class': lambda a: _("Crear impuesto: %(name)s (%(rate)s%%)") % {'name': a.get('name', ''), 'rate': a.get('rate', '')},
        'set_regional_config': lambda a: _("Configurar región y formato: %(config)s") % {'config': ', '.join(f'{k}={v}' for k, v in a.items() if v is not None)},
        'set_business_info': lambda a: _("Configurar datos del negocio: %(name)s") % {'name': a.get('business_name', '')},
        'set_tax_config': lambda a: _("Configurar impuestos: %(rate)s%% (incluido: %(included)s)") % {'rate': a.get('tax_rate', ''), 'included': a.get('tax_included', '')},
        'complete_setup_step': lambda a: _("Completar configuración inicial del hub"),
        'execute_plan': lambda a: _summarize_plan_steps(a.get('steps', [])),
        # Inventory
        'create_product': lambda a: _("Crear producto: %(name)s (%(price)s)") % {'name': a.get('name', ''), 'price': a.get('price', '')},
        'update_product': lambda a: _("Actualizar producto: %(id)s") % {'id': a.get('product_id', '')},
        'create_category': lambda a: _("Crear categoría: %(name)s") % {'name': a.get('name', '')},
        'adjust_stock': lambda a: _("Ajustar stock: %(qty)s uds. del producto %(id)s") % {'qty': a.get('quantity', ''), 'id': a.get('product_id', '')},
        'bulk_adjust_stock': lambda a: _("Ajustar stock masivo (%(count)s productos): %(reason)s") % {'count': len(a.get('items', [])), 'reason': a.get('reason', '')},
        # Customers
        'create_customer': lambda a: _("Crear cliente: %(name)s") % {'name': a.get('name', '')},
        'update_customer': lambda a: _("Actualizar cliente: %(id)s") % {'id': a.get('customer_id', '')},
        # Services
        'create_service': lambda a: _("Crear servicio: %(name)s (%(price)s)") % {'name': a.get('name', ''), 'price': a.get('price', '')},
        'create_service_category': lambda a: _("Crear categoría de servicio: %(name)s") % {'name': a.get('name', '')},
        'update_service': lambda a: _("Actualizar servicio: %(id)s") % {'id': a.get('service_id', '')},
        # Quotes
        'create_quote': lambda a: _("Crear presupuesto: %(title)s") % {'title': a.get('title', '')},
        'update_quote_status': lambda a: _("Cambiar estado del presupuesto %(id)s → %(action)s") % {'id': a.get('quote_id', ''), 'action': a.get('action', '')},
        # Leads
        'create_lead': lambda a: _("Crear lead: %(name)s (%(company)s)") % {'name': a.get('name', ''), 'company': a.get('company', '')},
        'move_lead_stage': lambda a: _("Mover lead %(id)s a la etapa %(stage)s") % {'id': a.get('lead_id', ''), 'stage': a.get('stage_id', '')},
        # Purchase Orders
        'create_purchase_order': lambda a: _("Crear orden de compra para proveedor %(id)s") % {'id': a.get('supplier_id', '')},
        # Appointments
        'create_appointment': lambda a: _("Reservar cita: %(customer)s el %(datetime)s") % {'customer': a.get('customer_name', ''), 'datetime': a.get('start_datetime', '')},
        # Expenses
        'create_expense': lambda a: _("Registrar gasto: %(title)s (%(amount)s)") % {'title': a.get('title', ''), 'amount': a.get('amount', '')},
        # Projects
        'create_project': lambda a: _("Crear proyecto: %(name)s") % {'name': a.get('name', '')},
        'log_time_entry': lambda a: _("Registrar %(hours)sh en el proyecto %(id)s") % {'hours': a.get('hours', ''), 'id': a.get('project_id', '')},
        # Support
        'create_ticket': lambda a: _("Crear ticket: %(subject)s") % {'subject': a.get('subject', '')},
        # Discounts
        'create_coupon': lambda a: _("Crear cupón: %(code)s (%(value)s%(type)s)") % {'code': a.get('code', ''), 'value': a.get('discount_value', ''), 'type': a.get('discount_type', '')},
        # Loyalty
        'award_loyalty_points': lambda a: _("Otorgar %(points)s puntos al miembro %(id)s") % {'points': a.get('points', ''), 'id': a.get('member_id', '')},
        # Shipping
        'create_shipment': lambda a: _("Crear envío para %(name)s") % {'name': a.get('recipient_name', '')},
        # Gift Cards
        'create_gift_card': lambda a: _("Crear tarjeta regalo: valor %(balance)s") % {'balance': a.get('initial_balance', '')},
        # Analytics
        'update_analytics_settings': lambda a: _("Actualizar configuración de analíticas"),
        # Pricing
        'create_price_list': lambda a: _("Crear lista de precios: %(name)s") % {'name': a.get('name', '')},
        'add_price_rule': lambda a: _("Añadir regla de precio a la lista %(id)s") % {'id': a.get('price_list_id', '')},
        # Accounting Sync
        'toggle_accounting_sync': lambda a: _("%(action)s sincronización contable: %(id)s") % {'action': _('Activar') if a.get('enabled') else _('Desactivar'), 'id': a.get('connection_id', '')},
        'trigger_accounting_sync': lambda a: _("Ejecutar sincronización contable: %(id)s") % {'id': a.get('connection_id', '')},
        # Reservations
        'create_reservation': lambda a: _("Crear reserva: %(name)s") % {'name': a.get('customer_name', '')},
        'update_reservation_status': lambda a: _("Cambiar estado de reserva %(id)s → %(status)s") % {'id': a.get('reservation_id', ''), 'status': a.get('status', '')},
        'create_time_slot': lambda a: _("Crear franja horaria: %(day)s %(start)s-%(end)s") % {'day': a.get('day_of_week', ''), 'start': a.get('start_time', ''), 'end': a.get('end_time', '')},
        'create_blocked_date': lambda a: _("Bloquear fecha: %(date)s") % {'date': a.get('date', '')},
        'update_reservation_settings': lambda a: _("Actualizar configuración de reservas"),
        'create_zone': lambda a: _("Crear zona: %(name)s") % {'name': a.get('name', '')},
        # Tables
        'create_table': lambda a: _("Crear mesa: %(name)s") % {'name': a.get('name', '')},
        'update_table': lambda a: _("Actualizar mesa: %(id)s") % {'id': a.get('table_id', '')},
        'bulk_create_tables': lambda a: _("Crear %(count)s mesas") % {'count': a.get('count', '')},
        'open_table_session': lambda a: _("Abrir sesión de mesa: %(id)s") % {'id': a.get('table_id', '')},
        # Attendance
        'create_attendance_record': lambda a: _("Registrar asistencia: empleado %(id)s") % {'id': a.get('employee_id', '')},
        # Maintenance
        'create_work_order': lambda a: _("Crear orden de trabajo: %(title)s") % {'title': a.get('title', a.get('description', '')[:50])},
        'create_maintenance_order': lambda a: _("Crear orden de mantenimiento: %(title)s") % {'title': a.get('title', '')},
        # Online Payments
        'create_payment_link': lambda a: _("Crear enlace de pago: %(amount)s") % {'amount': a.get('amount', '')},
        'create_payment_method': lambda a: _("Crear método de pago: %(name)s") % {'name': a.get('name', '')},
        # Accounting
        'create_account': lambda a: _("Crear cuenta contable: %(code)s %(name)s") % {'code': a.get('code', ''), 'name': a.get('name', '')},
        'create_journal_entry': lambda a: _("Crear asiento contable: %(desc)s") % {'desc': a.get('description', '')},
        # Feedback
        'create_feedback_form': lambda a: _("Crear formulario de valoración: %(title)s") % {'title': a.get('title', '')},
        # Manufacturing
        'create_bom': lambda a: _("Crear lista de materiales: %(name)s") % {'name': a.get('name', '')},
        'create_production_order': lambda a: _("Crear orden de producción: %(id)s") % {'id': a.get('bom_id', '')},
        # Reports
        'create_report': lambda a: _("Crear informe: %(name)s") % {'name': a.get('name', '')},
        # Messaging
        'create_message_template': lambda a: _("Crear plantilla de mensaje: %(name)s") % {'name': a.get('name', '')},
        'create_message_automation': lambda a: _("Crear automatización: %(name)s") % {'name': a.get('name', '')},
        # Approvals
        'approve_approval_request': lambda a: _("Aprobar solicitud: %(id)s") % {'id': a.get('request_id', '')},
        'reject_approval_request': lambda a: _("Rechazar solicitud: %(id)s") % {'id': a.get('request_id', '')},
        # Training
        'create_training_program': lambda a: _("Crear programa de formación: %(name)s") % {'name': a.get('name', '')},
        'enroll_employee_in_training': lambda a: _("Inscribir empleado %(emp)s en formación %(prog)s") % {'emp': a.get('employee_id', ''), 'prog': a.get('program_id', '')},
        # Returns
        'create_return_reason': lambda a: _("Crear motivo de devolución: %(name)s") % {'name': a.get('name', '')},
        # Assets
        'create_asset': lambda a: _("Crear activo: %(name)s") % {'name': a.get('name', '')},
        'create_asset_maintenance': lambda a: _("Programar mantenimiento del activo: %(id)s") % {'id': a.get('asset_id', '')},
        # Warehouse
        'create_warehouse': lambda a: _("Crear almacén: %(name)s") % {'name': a.get('name', '')},
        'create_warehouse_zone': lambda a: _("Crear zona de almacén: %(name)s") % {'name': a.get('name', '')},
        # Facturae
        'create_facturae_invoice': lambda a: _("Crear factura electrónica: %(id)s") % {'id': a.get('invoice_id', '')},
        'update_facturae_status': lambda a: _("Cambiar estado de Facturae %(id)s → %(action)s") % {'id': a.get('facturae_id', ''), 'action': a.get('action', '')},
        # Payroll
        'create_payslip': lambda a: _("Crear nómina: empleado %(id)s (%(period)s)") % {'id': a.get('employee_id', ''), 'period': a.get('period', '')},
        'update_payslip_status': lambda a: _("Cambiar estado de nómina %(id)s → %(action)s") % {'id': a.get('payslip_id', ''), 'action': a.get('action', '')},
        # Marketing Campaigns
        'create_marketing_campaign': lambda a: _("Crear campaña: %(name)s") % {'name': a.get('name', '')},
        # Commissions
        'create_commission_rule': lambda a: _("Crear regla de comisión: %(name)s") % {'name': a.get('name', '')},
        # E-Sign
        'create_signature_request': lambda a: _("Solicitar firma: %(doc)s") % {'doc': a.get('document_name', a.get('title', ''))},
        # Budgets
        'create_budget': lambda a: _("Crear presupuesto: %(name)s") % {'name': a.get('name', '')},
        # API Connect / Webhooks
        'create_webhook': lambda a: _("Crear webhook: %(url)s") % {'url': a.get('url', a.get('name', ''))},
        # Marketplace Connect
        'toggle_marketplace_sync': lambda a: _("%(action)s sincronización de marketplace: %(id)s") % {'action': _('Activar') if a.get('enabled') else _('Desactivar'), 'id': a.get('connection_id', '')},
        # Patient Records
        'create_patient': lambda a: _("Crear paciente: %(name)s") % {'name': a.get('name', '')},
        'create_treatment': lambda a: _("Crear tratamiento: %(name)s") % {'name': a.get('name', a.get('treatment_type', ''))},
        # Surveys
        'create_survey': lambda a: _("Crear encuesta: %(title)s") % {'title': a.get('title', '')},
        # Live Chat
        'assign_chat_conversation': lambda a: _("Asignar chat %(id)s al agente %(agent)s") % {'id': a.get('conversation_id', ''), 'agent': a.get('agent_id', '')},
        'close_chat_conversation': lambda a: _("Cerrar conversación de chat: %(id)s") % {'id': a.get('conversation_id', '')},
        'send_chat_message': lambda a: _("Enviar mensaje en conversación %(id)s") % {'id': a.get('conversation_id', '')},
        # Recruitment
        'create_job_position': lambda a: _("Crear puesto de trabajo: %(title)s") % {'title': a.get('title', '')},
        'create_candidate': lambda a: _("Crear candidato: %(name)s") % {'name': a.get('name', '')},
        # Multicurrency
        'add_currency': lambda a: _("Añadir moneda: %(code)s") % {'code': a.get('code', '')},
        'update_exchange_rate': lambda a: _("Actualizar tipo de cambio: %(id)s → %(rate)s") % {'id': a.get('currency_id', ''), 'rate': a.get('rate', '')},
        # Properties
        'create_property': lambda a: _("Crear propiedad: %(name)s") % {'name': a.get('name', '')},
        'create_tenant': lambda a: _("Crear inquilino: %(name)s") % {'name': a.get('name', '')},
        'create_lease': lambda a: _("Crear contrato de alquiler: propiedad %(id)s") % {'id': a.get('property_id', '')},
        # Tasks
        'create_task': lambda a: _("Crear tarea: %(title)s") % {'title': a.get('title', '')},
        'update_task_status': lambda a: _("Cambiar estado de tarea %(id)s → %(status)s") % {'id': a.get('task_id', ''), 'status': a.get('status', '')},
        # SII
        'create_sii_submission': lambda a: _("Crear envío SII: %(type)s (%(period)s)") % {'type': a.get('submission_type', ''), 'period': a.get('period', '')},
        # Schedules / Business Hours
        'set_business_hours': lambda a: _("Configurar horario comercial: %(day)s") % {'day': a.get('day_of_week', '')},
        'create_special_day': lambda a: _("Crear día especial: %(date)s") % {'date': a.get('date', '')},
        'bulk_set_business_hours': lambda a: _("Configurar horario comercial (%(count)s días)") % {'count': len(a.get('schedules', []))},
        # Notifications
        'mark_notifications_read': lambda a: _("Marcar notificaciones como leídas"),
        # Leave
        'create_leave_request': lambda a: _("Crear solicitud de ausencia: %(type)s (%(start)s - %(end)s)") % {'type': a.get('leave_type', ''), 'start': a.get('start_date', ''), 'end': a.get('end_date', '')},
        'approve_leave_request': lambda a: _("Aprobar solicitud de ausencia: %(id)s") % {'id': a.get('request_id', '')},
        'reject_leave_request': lambda a: _("Rechazar solicitud de ausencia: %(id)s") % {'id': a.get('request_id', '')},
        # Data Export
        'create_export_job': lambda a: _("Crear exportación: %(type)s (%(format)s)") % {'type': a.get('export_type', ''), 'format': a.get('format', '')},
        # Segments
        'create_segment': lambda a: _("Crear segmento: %(name)s") % {'name': a.get('name', '')},
        # GDPR
        'create_data_request': lambda a: _("Crear solicitud RGPD: %(type)s") % {'type': a.get('request_type', '')},
        # Staff
        'create_staff_member': lambda a: _("Crear miembro del equipo: %(name)s") % {'name': a.get('name', '')},
        'create_staff_role': lambda a: _("Crear rol de equipo: %(name)s") % {'name': a.get('name', '')},
        'create_time_off_request': lambda a: _("Crear solicitud de tiempo libre: %(id)s") % {'id': a.get('staff_id', '')},
        'assign_service_to_staff': lambda a: _("Asignar servicio %(service)s al empleado %(staff)s") % {'service': a.get('service_id', ''), 'staff': a.get('staff_id', '')},
        # Students / Course
        'create_student': lambda a: _("Crear alumno: %(name)s") % {'name': a.get('name', '')},
        'create_enrollment': lambda a: _("Crear matrícula: alumno %(id)s") % {'id': a.get('student_id', '')},
        'create_course': lambda a: _("Crear curso: %(name)s") % {'name': a.get('name', '')},
        # Fleet
        'create_vehicle': lambda a: _("Crear vehículo: %(name)s") % {'name': a.get('name', a.get('plate_number', ''))},
        'create_fuel_log': lambda a: _("Registrar repostaje: vehículo %(id)s") % {'id': a.get('vehicle_id', '')},
        # Referrals
        'create_referral': lambda a: _("Crear referido: %(name)s") % {'name': a.get('referrer_name', a.get('name', ''))},
        # Tax
        'create_tax_rate': lambda a: _("Crear tipo impositivo: %(name)s (%(rate)s%%)") % {'name': a.get('name', ''), 'rate': a.get('rate', '')},
        # Document Templates
        'create_document_template': lambda a: _("Crear plantilla de documento: %(name)s") % {'name': a.get('name', '')},
        # Contracts
        'create_contract': lambda a: _("Crear contrato: %(title)s") % {'title': a.get('title', '')},
        'update_contract_status': lambda a: _("Cambiar estado del contrato %(id)s → %(status)s") % {'id': a.get('contract_id', ''), 'status': a.get('status', '')},
        # Cash Register
        'create_cash_register': lambda a: _("Crear caja registradora: %(name)s") % {'name': a.get('name', '')},
        'close_cash_session': lambda a: _("Cerrar sesión de caja: %(id)s (saldo: %(balance)s)") % {'id': a.get('session_id', ''), 'balance': a.get('closing_balance', '')},
        # Orders / Kitchen
        'create_order': lambda a: _("Crear pedido: %(ref)s") % {'ref': a.get('table_id', a.get('customer_name', ''))},
        'update_order_status': lambda a: _("Cambiar estado del pedido %(id)s → %(status)s") % {'id': a.get('order_id', ''), 'status': a.get('status', '')},
        'create_kitchen_station': lambda a: _("Crear estación de cocina: %(name)s") % {'name': a.get('name', '')},
        'set_station_routing': lambda a: _("Configurar enrutamiento de estación: %(id)s") % {'id': a.get('station_id', '')},
        'update_orders_settings': lambda a: _("Actualizar configuración de pedidos"),
        'bump_order_item': lambda a: _("Marcar como listo: artículo %(id)s") % {'id': a.get('item_id', '')},
        'bump_order': lambda a: _("Marcar pedido como listo: %(id)s") % {'id': a.get('order_id', '')},
        'recall_order': lambda a: _("Recuperar pedido: %(id)s") % {'id': a.get('order_id', '')},
        'update_kitchen_settings': lambda a: _("Actualizar configuración de cocina"),
        # Email Marketing
        'create_email_template': lambda a: _("Crear plantilla de email: %(name)s") % {'name': a.get('name', '')},
        # Knowledge Base
        'create_kb_category': lambda a: _("Crear categoría de base de conocimiento: %(name)s") % {'name': a.get('name', '')},
        'create_kb_article': lambda a: _("Crear artículo de ayuda: %(title)s") % {'title': a.get('title', '')},
        # Quality
        'create_inspection': lambda a: _("Crear inspección: %(name)s") % {'name': a.get('name', a.get('title', ''))},
        # E-commerce
        'update_online_order_status': lambda a: _("Cambiar estado del pedido online %(id)s → %(status)s") % {'id': a.get('order_id', ''), 'status': a.get('status', '')},
        # Subscriptions
        'create_subscription': lambda a: _("Crear suscripción: cliente %(id)s") % {'id': a.get('customer_id', '')},
        'update_subscription_status': lambda a: _("Cambiar estado de suscripción %(id)s → %(status)s") % {'id': a.get('subscription_id', ''), 'status': a.get('status', '')},
        # Invoicing
        'create_invoice': lambda a: _("Crear factura: cliente %(id)s") % {'id': a.get('customer_id', '')},
        'update_invoice_status': lambda a: _("Cambiar estado de factura %(id)s → %(action)s") % {'id': a.get('invoice_id', ''), 'action': a.get('action', a.get('status', ''))},
        # Rentals
        'create_rental_item': lambda a: _("Crear artículo de alquiler: %(name)s") % {'name': a.get('name', '')},
        'create_rental': lambda a: _("Crear alquiler: cliente %(id)s") % {'id': a.get('customer_id', '')},
        # File Manager
        'create_folder': lambda a: _("Crear carpeta: %(name)s") % {'name': a.get('name', '')},
        # Online Booking
        'update_booking_status': lambda a: _("Cambiar estado de reserva online %(id)s → %(action)s") % {'id': a.get('booking_id', ''), 'action': a.get('action', '')},
        'create_online_booking': lambda a: _("Crear reserva online: %(name)s el %(date)s") % {'name': a.get('customer_name', ''), 'date': a.get('date', '')},
        # VoIP
        'add_call_notes': lambda a: _("Añadir notas a la llamada %(id)s") % {'id': a.get('call_id', '')},
        # Bank Sync
        'create_bank_account': lambda a: _("Crear cuenta bancaria: %(name)s") % {'name': a.get('name', '')},
        # Bulk operations
        'bulk_create_employees': lambda a: _("Crear %(count)s empleados: %(names)s") % {'count': len(a.get('employees', [])), 'names': ', '.join(e.get('first_name', '') + ' ' + e.get('last_name', '') for e in a.get('employees', []))},
    }

    formatter = descriptions.get(tool_name)
    if formatter:
        try:
            return formatter(tool_args)
        except Exception:
            pass

    # Generic fallback — stringify values to avoid join errors with dicts/lists
    args_str = ', '.join(f'{k}={v!r}' if not isinstance(v, str) else f'{k}={v}' for k, v in tool_args.items() if v is not None)
    return f"{tool_name}({args_str})"


# ============================================================================
# PLAN PAGE
# ============================================================================

def _get_plan_data(hub_jwt):
    """
    Fetch assistant config (current tier + usage) and all available tiers from Cloud.

    Returns dict with:
    - current_tier, tier_name, usage (messages_used, messages_limit)
    - tiers: list of all available tiers with pricing
    - subscription_status, period_end
    """
    import requests as http_requests
    from django.conf import settings

    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')
    headers = {'Authorization': f'Bearer {hub_jwt}', 'Accept': 'application/json'}
    data = {
        'current_tier': 'free',
        'tier_name': 'Free',
        'usage': {'messages_used': 0, 'messages_limit': 30},
        'tiers': [],
        'has_subscription': False,
        'subscription_status': None,
    }

    # 1. Get current config + usage
    try:
        resp = http_requests.get(
            f"{base_url}/api/hubs/me/assistant/config/",
            headers=headers,
            timeout=5,
        )
        if resp.status_code == 200:
            config = resp.json()
            data['current_tier'] = config.get('tier', 'free')
            data['tier_name'] = config.get('tier_name', 'Free')
            data['has_subscription'] = config.get('has_subscription', False)
            data['usage'] = config.get('usage', data['usage'])
    except Exception:
        pass

    # 2. Get all tiers from marketplace module listing
    try:
        resp = http_requests.get(
            f"{base_url}/api/marketplace/modules/",
            headers={'X-Hub-Token': hub_jwt, 'Accept': 'application/json'},
            params={'module_id': 'assistant'},
            timeout=5,
        )
        if resp.status_code == 200:
            modules = resp.json()
            results = modules.get('results', modules) if isinstance(modules, dict) else modules
            for mod in results:
                if mod.get('module_id') == 'assistant':
                    data['tiers'] = mod.get('assistant_tiers') or []
                    data['cloud_module_id'] = str(mod.get('id', ''))
                    break
    except Exception:
        pass

    # 3. Get subscription status (period end, cancel info)
    try:
        resp = http_requests.get(
            f"{base_url}/api/hubs/me/module-subscription/",
            params={'module': 'assistant'},
            headers=headers,
            timeout=5,
        )
        if resp.status_code == 200:
            sub = resp.json()
            data['subscription_status'] = sub.get('status')
            data['period_end'] = sub.get('period_end')
            data['trial_end'] = sub.get('trial_end')
            data['cancel_at_period_end'] = sub.get('cancel_at_period_end', False)
    except Exception:
        pass

    return data


@login_required
@permission_required('assistant.use_chat')
@with_module_nav('assistant', 'plan')
@htmx_view('assistant/pages/plan.html', 'assistant/partials/plan_content.html')
def plan_page(request):
    """AI Assistant plan management — view current plan, usage, upgrade/cancel."""
    from apps.configuration.models import HubConfig

    hub_config = HubConfig.get_config()
    plan_data = _get_plan_data(hub_config.hub_jwt)

    # Build tier list with current/recommended flags
    current_tier = plan_data['current_tier']
    tier_order = {'free': 0, 'basic': 1, 'pro': 2, 'enterprise': 3}
    current_order = tier_order.get(current_tier, 0)

    tiers = []
    for t in plan_data.get('tiers', []):
        slug = t.get('slug', '')
        order = tier_order.get(slug, 99)
        tiers.append({
            **t,
            'is_current': slug == current_tier,
            'is_upgrade': order > current_order,
            'is_downgrade': order < current_order and slug != 'free',
        })

    usage = plan_data.get('usage', {})
    messages_used = usage.get('messages_used', 0)
    messages_limit = usage.get('messages_limit', 30)
    usage_pct = min(round((messages_used / messages_limit) * 100), 100) if messages_limit > 0 else 0

    return {
        'current_tier': current_tier,
        'tier_name': plan_data['tier_name'],
        'tiers': tiers,
        'cloud_module_id': plan_data.get('cloud_module_id', ''),
        'messages_used': messages_used,
        'messages_limit': messages_limit,
        'usage_pct': usage_pct,
        'has_subscription': plan_data['has_subscription'] and current_tier != 'free',
        'subscription_status': plan_data.get('subscription_status'),
        'period_end': plan_data.get('period_end'),
        'trial_end': plan_data.get('trial_end'),
        'cancel_at_period_end': plan_data.get('cancel_at_period_end', False),
    }


@login_required
@permission_required('assistant.use_chat')
def download_file(request, file_id):
    """Download an assistant-generated file via S3 presigned URL."""
    from django.http import Http404
    from django.shortcuts import redirect

    try:
        f = AssistantFile.objects.get(
            id=file_id,
            conversation__user_id=request.session.get('local_user_id'),
        )
    except AssistantFile.DoesNotExist:
        raise Http404

    from django.core.files.storage import default_storage
    url = default_storage.url(f.s3_key)
    return redirect(url)


@login_required
@require_POST
def skip_setup(request):
    """Skip AI setup — mark hub as configured with minimal defaults."""
    from django.shortcuts import redirect
    from apps.configuration.models import HubConfig, StoreConfig

    hub_config = HubConfig.get_solo()
    store_config = StoreConfig.get_solo()
    hub_config.is_configured = True
    hub_config.save()
    store_config.is_configured = True
    store_config.save()
    return redirect('/')
