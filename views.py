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

from apps.accounts.decorators import login_required, permission_required
from apps.modules_runtime.navigation import with_module_nav
from apps.core.htmx import htmx_view

from .models import AssistantConversation, AssistantActionLog
from .prompts import build_system_prompt
from .tools import get_tools_for_context, get_tool
from .feedback import record_feedback

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10
MAX_IDENTICAL_CALLS = 2  # max times same tool+args can repeat
PROGRESS_CACHE_TIMEOUT = 120  # seconds


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

@login_required
@permission_required('assistant.use_chat')
@with_module_nav('assistant', 'chat')
@htmx_view('assistant/pages/chat.html', 'assistant/partials/chat_panel.html')
def _get_tier_features(hub_jwt):
    """Fetch assistant tier features from Cloud. Returns feature list or empty list on error."""
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
            return resp.json().get('features', [])
    except Exception:
        pass
    return []


def chat_page(request):
    """Main chat page."""
    conversations = AssistantConversation.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).order_by('-updated_at')[:10]

    last_conversation = conversations[0] if conversations else None

    # Detect setup context (redirected from StoreConfigCheckMiddleware)
    from apps.configuration.models import HubConfig
    hub_config = HubConfig.get_config()
    context = 'setup' if (request.GET.get('context') == 'setup' or not hub_config.is_configured) else 'general'

    # Get tier features to show/hide UI elements
    tier_features = _get_tier_features(hub_config.hub_jwt)
    can_attach = 'files' in tier_features or 'images' in tier_features

    return {
        'conversations': conversations,
        'last_conversation': last_conversation,
        'chat_context': context,
        'is_setup_mode': context == 'setup',
        'can_attach': can_attach,
    }


@login_required
@permission_required('assistant.view_logs')
@with_module_nav('assistant', 'history')
@htmx_view('assistant/pages/history.html', 'assistant/partials/history_content.html')
def history_page(request):
    """Conversation history page."""
    conversations = AssistantConversation.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).order_by('-updated_at')[:50]

    return {
        'conversations': conversations,
    }


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


def _set_progress(request_id, event_type, data=''):
    """Update progress for a polling request."""
    if request_id:
        cache.set(
            f'assistant_progress_{request_id}',
            {'type': event_type, 'data': data},
            timeout=PROGRESS_CACHE_TIMEOUT,
        )


def run_agentic_loop(user, conversation, ai_input, context, request,
                     request_id=None):
    """
    Run the agentic tool-calling loop.

    Shared between the HTMX chat view and the REST API.

    Args:
        user: LocalUser instance
        conversation: AssistantConversation instance
        ai_input: str or list (text message or multimodal input)
        context: 'general' or 'setup'
        request: Django request (for session, building prompts)
        request_id: optional ID for progress tracking via polling

    Returns:
        dict with keys:
            response_text: str - The assistant's text response
            pending_actions: list of dicts with log_id, tool_name, tool_args, description
            conversation_id: int
            tier_info: dict or None
    """
    user_id = str(user.id)
    original_message = ai_input if isinstance(ai_input, str) else ''
    instructions = build_system_prompt(request, context)

    # Dynamic tool loading: restore from session so loaded state persists between messages.
    # Only keep modules that are still active (prevents stale refs after uninstall).
    from assistant.tools import _get_active_module_ids
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

    tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)

    response_text = ""
    pending_actions = []
    tier_info = None
    call_counts = {}  # {call_hash: count} for anti-loop detection

    _set_progress(request_id, 'thinking', 'Analyzing your request...')

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
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
                _set_progress(request_id, 'tool', f'Using {tool_name}...')
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
                            request.session.modified = True
                        # Unload: when unload_module_tools is called, remove from session
                        elif tool_name == 'unload_module_tools' and isinstance(result, dict) and 'error' not in result:
                            for mid in result.get('unloaded', tool_args.get('modules', [])):
                                loaded_modules.discard(mid)
                            tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)
                            request.session['assistant_loaded_modules'] = list(loaded_modules)
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
        response_text = "Sorry, I couldn't generate a response. Please try again or rephrase your request."

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
            error_msg = '; '.join(errors) if errors else 'Action completed with errors'
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
                'content': 'Please type a message.',
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

    # Generate a request_id for progress tracking
    request_id = uuid_mod.uuid4().hex[:16]

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
            )
            # Store the completed result
            cache.set(f'assistant_result_{request_id}', result, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'complete', '')
        except AgenticLoopError as e:
            # AgenticLoopError has user-facing messages (already sanitized)
            cache.set(f'assistant_result_{request_id}', {'error': str(e)}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', str(e))
        except Exception as e:
            logger.error(f"[ASSISTANT] Background error: {e}", exc_info=True)
            # Show friendly message to user, log technical details
            friendly_msg = "Something went wrong. Please try again or start a new conversation."
            cache.set(f'assistant_result_{request_id}', {'error': friendly_msg}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', friendly_msg)

    # Start background thread
    thread = threading.Thread(target=_background_task, daemon=True)
    thread.start()

    # Return polling partial
    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': 'Analyzing your request...',
    }, request=request)

    response = HttpResponse(html)
    response['X-Conversation-Id'] = str(conversation.id)
    return response


@login_required
@permission_required('assistant.use_chat')
@require_POST
def chat_stream(request):
    """
    SSE streaming endpoint for the chat assistant.

    Proxies the Cloud streaming endpoint (POST /api/hubs/me/assistant/chat/stream/)
    directly to the browser as Server-Sent Events.

    The stream carries:
      data: {"type": "text_delta", "text": "..."}
      data: {"type": "function_call", "name": "...", "call_id": "...", "arguments": "..."}
      data: {"type": "response", "id": "...", "output": [...], "usage": {...}}
      data: {"type": "error", "message": "..."}
      data: [DONE]

    After the stream ends (on "response" event), the frontend checks for
    function_calls in output. If present, it falls back to the standard
    polling flow by posting to chat_message. Otherwise the streamed text
    is the final answer.

    Returns 200 text/event-stream on success, 400/500 JSON on setup errors.
    """
    import requests as http_requests
    from apps.configuration.models import HubConfig
    from django.conf import settings

    message = request.POST.get('message', '').strip()
    conversation_id = request.POST.get('conversation_id', '')
    context = request.POST.get('context', 'general')

    if not message:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": "Please type a message."})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    user_id = request.session.get('local_user_id')
    conversation = _get_or_create_conversation(user_id, conversation_id, context)

    from apps.accounts.models import LocalUser
    try:
        user = LocalUser.objects.get(id=user_id)
    except LocalUser.DoesNotExist:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": "User not found."})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    _track_conversation_message(conversation, message)

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        def _err():
            yield f'data: {json.dumps({"type": "error", "message": "Hub is not connected to Cloud."})}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingHttpResponse(_err(), content_type='text/event-stream')

    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com').rstrip('/')
    instructions = build_system_prompt(request, context)

    # Build tools list for the payload
    from assistant.tools import _get_active_module_ids
    active_module_ids = set(_get_active_module_ids())
    is_new_session = conversation.message_count == 1  # just incremented above
    if is_new_session:
        loaded_modules = set()
    else:
        session_loaded = request.session.get('assistant_loaded_modules', [])
        loaded_modules = set(session_loaded) & active_module_ids

    tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)

    payload = {
        'input': message,
        'instructions': instructions,
        'conversation_id': str(conversation.id),
        'tools': tools,
    }
    if is_new_session:
        payload['new_session'] = True

    conversation_id_str = str(conversation.id)

    def _proxy_stream():
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

                # Inject conversation_id as first event so frontend can update its state
                yield f'data: {json.dumps({"type": "conv_id", "conversation_id": conversation_id_str})}\n\n'

                # Pass-through SSE lines from Cloud
                for line in resp.iter_lines(decode_unicode=True):
                    if line:
                        yield line + '\n\n'
                    # Empty lines are SSE delimiters — forward them
                    # (iter_lines strips them, so we add \n\n after each data line above)

        except http_requests.exceptions.Timeout:
            yield f'data: {json.dumps({"type": "error", "message": "Request timed out. Please try again."})}\n\n'
            yield 'data: [DONE]\n\n'
        except Exception as e:
            logger.error(f"[ASSISTANT STREAM] Proxy error: {e}", exc_info=True)
            yield f'data: {json.dumps({"type": "error", "message": "Connection error. Please try again."})}\n\n'
            yield 'data: [DONE]\n\n'

    response = StreamingHttpResponse(_proxy_stream(), content_type='text/event-stream')
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
            # Neither progress nor result — request expired or was lost. Stop polling.
            return HttpResponse(render_to_string('assistant/partials/message.html', {
                'role': 'system',
                'content': 'Request timed out. Please try again.',
                'error': True,
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
            'message': progress.get('data', 'Processing...'),
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
            'content': 'Action not found or already processed.',
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

    _set_progress(request_id, 'tool', 'Starting plan execution...')

    def _run_plan():
        from django.test import RequestFactory
        fake_request = RequestFactory().get('/')
        fake_request.session = session_data
        try:
            plan_result = execute_confirmed_action(
                action_log, fake_request, plan_request_id=request_id,
            )
            if plan_result['success']:
                # Build resume_input and continue the agentic loop
                call_id = action_log.tool_args.get('_call_id', '')
                if call_id:
                    resume_input = [{
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _json_dumps(plan_result.get('result', {})),
                    }]
                else:
                    resume_input = (
                        f"[execute_plan completed. "
                        f"Result: {_json_dumps(plan_result.get('result', {}))}] "
                        f"Continue with the next steps."
                    )
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
                    logger.error(f"[ASSISTANT] Plan resume loop error: {e}", exc_info=True)
                    cache.set(f'assistant_result_{request_id}', {'error': 'Something went wrong.'}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', 'Something went wrong.')
            else:
                # Plan failed — build an error message with rollback info
                err_msg = plan_result.get('message', 'Plan execution failed.')
                plan_exec_result = plan_result.get('result', {})
                rolled_back = plan_exec_result.get('rolled_back', [])
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
        except Exception as e:
            logger.error(f"[ASSISTANT] execute_plan async error: {e}", exc_info=True)
            cache.set(f'assistant_result_{request_id}', {'error': 'Something went wrong.'}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', 'Something went wrong.')

    thread = threading.Thread(target=_run_plan, daemon=True)
    thread.start()

    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': 'Executing plan...',
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
            cache.set(f'assistant_result_{request_id}', {'error': 'Something went wrong.'}, timeout=PROGRESS_CACHE_TIMEOUT)
            _set_progress(request_id, 'error', 'Something went wrong.')

    thread = threading.Thread(target=_resume_loop, daemon=True)
    thread.start()

    # Return a polling partial so the frontend waits for the resumed loop
    html = render_to_string('assistant/partials/progress.html', {
        'request_id': request_id,
        'message': 'Continuing setup...',
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
                    cache.set(f'assistant_result_{request_id}', {'error': 'Something went wrong.'}, timeout=PROGRESS_CACHE_TIMEOUT)
                    _set_progress(request_id, 'error', 'Something went wrong.')

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
    # Actions with their own named summary
    named = {
        'set_regional_config': 'set region',
        'set_business_info': None,  # will use business_name
        'set_tax_config': None,     # will use rate
        'complete_setup': 'complete setup',
        'install_blueprint': None,  # will use type_codes
        'install_blueprint_products': None,  # will use business_type
    }
    # Countable actions
    counts = Counter()
    parts = []
    seen_named = []

    for step in steps:
        action = step.get('action', '')
        params = step.get('params', {})

        if action == 'set_business_info':
            name = params.get('business_name', '')
            seen_named.append(f"set business: {name}" if name else 'set business info')
        elif action == 'set_tax_config':
            rate = params.get('tax_rate', '')
            seen_named.append(f"set tax {rate}%" if rate else 'set tax config')
        elif action == 'set_regional_config':
            seen_named.append('set region')
        elif action == 'install_blueprint':
            type_codes = params.get('type_codes', [])
            label = ', '.join(type_codes) if type_codes else 'blueprint'
            seen_named.append(f"install blueprint ({label})")
        elif action == 'install_blueprint_products':
            bt = params.get('business_type', '')
            seen_named.append(f"import products ({bt})" if bt else 'import products')
        elif action == 'complete_setup':
            seen_named.append('complete setup')
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
        'create_role': 'role', 'create_employee': 'employee',
        'create_tax_class': 'tax class', 'create_category': 'category',
        'create_product': 'product', 'create_service': 'service',
        'create_service_category': 'service category',
        'create_payment_method': 'payment method',
        'create_zone': 'zone', 'create_table': 'table',
        'create_station': 'station', 'set_business_hours': 'business hours',
        'bulk_create_zones': 'zone batch', 'bulk_create_tables': 'table batch',
        'update_store_config': 'store update',
    }
    for action, count in counts.items():
        label = label_map.get(action, action.replace('_', ' '))
        if count == 1:
            parts.append(label)
        else:
            parts.append(f"{count} {label}s")

    all_parts = seen_named + parts
    total = len(steps)
    summary = ', '.join(all_parts) if all_parts else f"{total} steps"
    return f"Plan ({total} steps): {summary}"


def format_confirmation_text(tool_name, tool_args):
    """Format a human-readable description of the pending action."""
    descriptions = {
        # Hub core tools
        'update_store_config': lambda a: f"Update store: {', '.join(k for k, v in a.items() if v is not None)}",
        'select_blocks': lambda a: f"Select blocks: {', '.join(a.get('block_slugs', []))}",
        'enable_module': lambda a: f"Enable module: {a.get('module_id', '')}",
        'disable_module': lambda a: f"Disable module: {a.get('module_id', '')}",
        'create_role': lambda a: f"Create role: {a.get('display_name', a.get('name', ''))}",
        'create_employee': lambda a: f"Create employee: {a.get('name', '')} ({a.get('role_name', '')})",
        'create_tax_class': lambda a: f"Create tax: {a.get('name', '')} ({a.get('rate', '')}%)",
        'set_regional_config': lambda a: f"Set region: {', '.join(f'{k}={v}' for k, v in a.items() if v is not None)}",
        'set_business_info': lambda a: f"Set business: {a.get('business_name', '')}",
        'set_tax_config': lambda a: f"Set tax: {a.get('tax_rate', '')}% (included: {a.get('tax_included', '')})",
        'complete_setup_step': lambda a: "Complete hub setup",
        'execute_plan': lambda a: _summarize_plan_steps(a.get('steps', [])),
        # Inventory
        'create_product': lambda a: f"Create product: {a.get('name', '')} ({a.get('price', '')})",
        'update_product': lambda a: f"Update product: {a.get('product_id', '')}",
        'create_category': lambda a: f"Create category: {a.get('name', '')}",
        'adjust_stock': lambda a: f"Adjust stock: {a.get('quantity', '')} units for product {a.get('product_id', '')}",
        'bulk_adjust_stock': lambda a: f"Bulk adjust stock ({len(a.get('items', []))} products): {a.get('reason', '')}",
        # Customers
        'create_customer': lambda a: f"Create customer: {a.get('name', '')}",
        'update_customer': lambda a: f"Update customer: {a.get('customer_id', '')}",
        # Services
        'create_service': lambda a: f"Create service: {a.get('name', '')} ({a.get('price', '')})",
        'create_service_category': lambda a: f"Create service category: {a.get('name', '')}",
        'update_service': lambda a: f"Update service: {a.get('service_id', '')}",
        # Quotes
        'create_quote': lambda a: f"Create quote: {a.get('title', '')}",
        'update_quote_status': lambda a: f"Update quote {a.get('quote_id', '')} → {a.get('action', '')}",
        # Leads
        'create_lead': lambda a: f"Create lead: {a.get('name', '')} ({a.get('company', '')})",
        'move_lead_stage': lambda a: f"Move lead {a.get('lead_id', '')} to stage {a.get('stage_id', '')}",
        # Purchase Orders
        'create_purchase_order': lambda a: f"Create purchase order for supplier {a.get('supplier_id', '')}",
        # Appointments
        'create_appointment': lambda a: f"Book appointment: {a.get('customer_name', '')} at {a.get('start_datetime', '')}",
        # Expenses
        'create_expense': lambda a: f"Record expense: {a.get('title', '')} ({a.get('amount', '')})",
        # Projects
        'create_project': lambda a: f"Create project: {a.get('name', '')}",
        'log_time_entry': lambda a: f"Log {a.get('hours', '')}h on project {a.get('project_id', '')}",
        # Support
        'create_ticket': lambda a: f"Create ticket: {a.get('subject', '')}",
        # Discounts
        'create_coupon': lambda a: f"Create coupon: {a.get('code', '')} ({a.get('discount_value', '')}{a.get('discount_type', '')})",
        # Loyalty
        'award_loyalty_points': lambda a: f"Award {a.get('points', '')} points to member {a.get('member_id', '')}",
        # Shipping
        'create_shipment': lambda a: f"Create shipment to {a.get('recipient_name', '')}",
        # Gift Cards
        'create_gift_card': lambda a: f"Create gift card: {a.get('initial_balance', '')} value",
        # Analytics
        'update_analytics_settings': lambda a: f"Update analytics settings",
        # Pricing
        'create_price_list': lambda a: f"Create price list: {a.get('name', '')}",
        'add_price_rule': lambda a: f"Add price rule to list {a.get('price_list_id', '')}",
        # Accounting Sync
        'toggle_accounting_sync': lambda a: f"{'Enable' if a.get('enabled') else 'Disable'} accounting sync: {a.get('connection_id', '')}",
        'trigger_accounting_sync': lambda a: f"Trigger accounting sync: {a.get('connection_id', '')}",
        # Reservations
        'create_reservation': lambda a: f"Create reservation: {a.get('customer_name', '')}",
        'update_reservation_status': lambda a: f"Update reservation {a.get('reservation_id', '')} → {a.get('status', '')}",
        'create_time_slot': lambda a: f"Create time slot: {a.get('day_of_week', '')} {a.get('start_time', '')}-{a.get('end_time', '')}",
        'create_blocked_date': lambda a: f"Block date: {a.get('date', '')}",
        'update_reservation_settings': lambda a: f"Update reservation settings",
        'create_zone': lambda a: f"Create zone: {a.get('name', '')}",
        # Tables
        'create_table': lambda a: f"Create table: {a.get('name', '')}",
        'update_table': lambda a: f"Update table: {a.get('table_id', '')}",
        'bulk_create_tables': lambda a: f"Create {a.get('count', '')} tables",
        'open_table_session': lambda a: f"Open table session: {a.get('table_id', '')}",
        # Attendance
        'create_attendance_record': lambda a: f"Record attendance: {a.get('employee_id', '')}",
        # Maintenance
        'create_work_order': lambda a: f"Create work order: {a.get('title', a.get('description', '')[:50])}",
        'create_maintenance_order': lambda a: f"Create maintenance order: {a.get('title', '')}",
        # Online Payments
        'create_payment_link': lambda a: f"Create payment link: {a.get('amount', '')}",
        'create_payment_method': lambda a: f"Create payment method: {a.get('name', '')}",
        # Accounting
        'create_account': lambda a: f"Create account: {a.get('code', '')} {a.get('name', '')}",
        'create_journal_entry': lambda a: f"Create journal entry: {a.get('description', '')}",
        # Feedback
        'create_feedback_form': lambda a: f"Create feedback form: {a.get('title', '')}",
        # Manufacturing
        'create_bom': lambda a: f"Create BOM: {a.get('name', '')}",
        'create_production_order': lambda a: f"Create production order: {a.get('bom_id', '')}",
        # Reports
        'create_report': lambda a: f"Create report: {a.get('name', '')}",
        # Messaging
        'create_message_template': lambda a: f"Create message template: {a.get('name', '')}",
        'create_message_automation': lambda a: f"Create automation: {a.get('name', '')}",
        # Approvals
        'approve_approval_request': lambda a: f"Approve request: {a.get('request_id', '')}",
        'reject_approval_request': lambda a: f"Reject request: {a.get('request_id', '')}",
        # Training
        'create_training_program': lambda a: f"Create training: {a.get('name', '')}",
        'enroll_employee_in_training': lambda a: f"Enroll employee {a.get('employee_id', '')} in training {a.get('program_id', '')}",
        # Returns
        'create_return_reason': lambda a: f"Create return reason: {a.get('name', '')}",
        # Assets
        'create_asset': lambda a: f"Create asset: {a.get('name', '')}",
        'create_asset_maintenance': lambda a: f"Schedule maintenance: {a.get('asset_id', '')}",
        # Warehouse
        'create_warehouse': lambda a: f"Create warehouse: {a.get('name', '')}",
        'create_warehouse_zone': lambda a: f"Create warehouse zone: {a.get('name', '')}",
        # Facturae
        'create_facturae_invoice': lambda a: f"Create Facturae invoice: {a.get('invoice_id', '')}",
        'update_facturae_status': lambda a: f"Update Facturae {a.get('facturae_id', '')} → {a.get('action', '')}",
        # Payroll
        'create_payslip': lambda a: f"Create payslip: employee {a.get('employee_id', '')} ({a.get('period', '')})",
        'update_payslip_status': lambda a: f"Update payslip {a.get('payslip_id', '')} → {a.get('action', '')}",
        # Marketing Campaigns
        'create_marketing_campaign': lambda a: f"Create campaign: {a.get('name', '')}",
        # Commissions
        'create_commission_rule': lambda a: f"Create commission rule: {a.get('name', '')}",
        # E-Sign
        'create_signature_request': lambda a: f"Request signature: {a.get('document_name', a.get('title', ''))}",
        # Budgets
        'create_budget': lambda a: f"Create budget: {a.get('name', '')}",
        # API Connect / Webhooks
        'create_webhook': lambda a: f"Create webhook: {a.get('url', a.get('name', ''))}",
        # Marketplace Connect
        'toggle_marketplace_sync': lambda a: f"{'Enable' if a.get('enabled') else 'Disable'} marketplace sync: {a.get('connection_id', '')}",
        # Patient Records
        'create_patient': lambda a: f"Create patient: {a.get('name', '')}",
        'create_treatment': lambda a: f"Create treatment: {a.get('name', a.get('treatment_type', ''))}",
        # Surveys
        'create_survey': lambda a: f"Create survey: {a.get('title', '')}",
        # Live Chat
        'assign_chat_conversation': lambda a: f"Assign chat {a.get('conversation_id', '')} to agent {a.get('agent_id', '')}",
        'close_chat_conversation': lambda a: f"Close chat conversation: {a.get('conversation_id', '')}",
        'send_chat_message': lambda a: f"Send chat message in conversation {a.get('conversation_id', '')}",
        # Recruitment
        'create_job_position': lambda a: f"Create job position: {a.get('title', '')}",
        'create_candidate': lambda a: f"Create candidate: {a.get('name', '')}",
        # Multicurrency
        'add_currency': lambda a: f"Add currency: {a.get('code', '')}",
        'update_exchange_rate': lambda a: f"Update exchange rate: {a.get('currency_id', '')} → {a.get('rate', '')}",
        # Properties
        'create_property': lambda a: f"Create property: {a.get('name', '')}",
        'create_tenant': lambda a: f"Create tenant: {a.get('name', '')}",
        'create_lease': lambda a: f"Create lease: property {a.get('property_id', '')}",
        # Tasks
        'create_task': lambda a: f"Create task: {a.get('title', '')}",
        'update_task_status': lambda a: f"Update task {a.get('task_id', '')} → {a.get('status', '')}",
        # SII
        'create_sii_submission': lambda a: f"Create SII submission: {a.get('submission_type', '')} ({a.get('period', '')})",
        # Schedules / Business Hours
        'set_business_hours': lambda a: f"Set business hours: {a.get('day_of_week', '')}",
        'create_special_day': lambda a: f"Create special day: {a.get('date', '')}",
        'bulk_set_business_hours': lambda a: f"Set business hours ({len(a.get('schedules', []))} days)",
        # Notifications
        'mark_notifications_read': lambda a: f"Mark notifications as read",
        # Leave
        'create_leave_request': lambda a: f"Create leave request: {a.get('leave_type', '')} ({a.get('start_date', '')} - {a.get('end_date', '')})",
        'approve_leave_request': lambda a: f"Approve leave request: {a.get('request_id', '')}",
        'reject_leave_request': lambda a: f"Reject leave request: {a.get('request_id', '')}",
        # Data Export
        'create_export_job': lambda a: f"Create export job: {a.get('export_type', '')} ({a.get('format', '')})",
        # Segments
        'create_segment': lambda a: f"Create segment: {a.get('name', '')}",
        # GDPR
        'create_data_request': lambda a: f"Create GDPR request: {a.get('request_type', '')}",
        # Staff
        'create_staff_member': lambda a: f"Create staff member: {a.get('name', '')}",
        'create_staff_role': lambda a: f"Create staff role: {a.get('name', '')}",
        'create_time_off_request': lambda a: f"Create time off request: {a.get('staff_id', '')}",
        'assign_service_to_staff': lambda a: f"Assign service {a.get('service_id', '')} to staff {a.get('staff_id', '')}",
        # Students / Course
        'create_student': lambda a: f"Create student: {a.get('name', '')}",
        'create_enrollment': lambda a: f"Create enrollment: student {a.get('student_id', '')}",
        'create_course': lambda a: f"Create course: {a.get('name', '')}",
        # Fleet
        'create_vehicle': lambda a: f"Create vehicle: {a.get('name', a.get('plate_number', ''))}",
        'create_fuel_log': lambda a: f"Log fuel: vehicle {a.get('vehicle_id', '')}",
        # Referrals
        'create_referral': lambda a: f"Create referral: {a.get('referrer_name', a.get('name', ''))}",
        # Tax
        'create_tax_rate': lambda a: f"Create tax rate: {a.get('name', '')} ({a.get('rate', '')}%)",
        # Document Templates
        'create_document_template': lambda a: f"Create template: {a.get('name', '')}",
        # Contracts
        'create_contract': lambda a: f"Create contract: {a.get('title', '')}",
        'update_contract_status': lambda a: f"Update contract {a.get('contract_id', '')} → {a.get('status', '')}",
        # Cash Register
        'create_cash_register': lambda a: f"Create cash register: {a.get('name', '')}",
        'close_cash_session': lambda a: f"Close cash session: {a.get('session_id', '')} (closing balance: {a.get('closing_balance', '')})",
        # Orders / Kitchen
        'create_order': lambda a: f"Create order: {a.get('table_id', a.get('customer_name', ''))}",
        'update_order_status': lambda a: f"Update order {a.get('order_id', '')} → {a.get('status', '')}",
        'create_kitchen_station': lambda a: f"Create kitchen station: {a.get('name', '')}",
        'set_station_routing': lambda a: f"Set station routing: {a.get('station_id', '')}",
        'update_orders_settings': lambda a: f"Update orders settings",
        'bump_order_item': lambda a: f"Bump order item: {a.get('item_id', '')}",
        'bump_order': lambda a: f"Bump order: {a.get('order_id', '')}",
        'recall_order': lambda a: f"Recall order: {a.get('order_id', '')}",
        'update_kitchen_settings': lambda a: f"Update kitchen settings",
        # Email Marketing
        'create_email_template': lambda a: f"Create email template: {a.get('name', '')}",
        # Knowledge Base
        'create_kb_category': lambda a: f"Create KB category: {a.get('name', '')}",
        'create_kb_article': lambda a: f"Create KB article: {a.get('title', '')}",
        # Quality
        'create_inspection': lambda a: f"Create inspection: {a.get('name', a.get('title', ''))}",
        # E-commerce
        'update_online_order_status': lambda a: f"Update online order {a.get('order_id', '')} → {a.get('status', '')}",
        # Subscriptions
        'create_subscription': lambda a: f"Create subscription: {a.get('customer_id', '')}",
        'update_subscription_status': lambda a: f"Update subscription {a.get('subscription_id', '')} → {a.get('status', '')}",
        # Invoicing
        'create_invoice': lambda a: f"Create invoice: {a.get('customer_id', '')}",
        'update_invoice_status': lambda a: f"Update invoice {a.get('invoice_id', '')} → {a.get('action', a.get('status', ''))}",
        # Rentals
        'create_rental_item': lambda a: f"Create rental item: {a.get('name', '')}",
        'create_rental': lambda a: f"Create rental: {a.get('customer_id', '')}",
        # File Manager
        'create_folder': lambda a: f"Create folder: {a.get('name', '')}",
        # Online Booking
        'update_booking_status': lambda a: f"Update booking {a.get('booking_id', '')} → {a.get('action', '')}",
        'create_online_booking': lambda a: f"Create booking: {a.get('customer_name', '')} on {a.get('date', '')}",
        # VoIP
        'add_call_notes': lambda a: f"Add notes to call {a.get('call_id', '')}",
        # Bank Sync
        'create_bank_account': lambda a: f"Create bank account: {a.get('name', '')}",
    }

    formatter = descriptions.get(tool_name)
    if formatter:
        try:
            return formatter(tool_args)
        except Exception:
            pass

    # Generic fallback
    args_str = ', '.join(f'{k}={v}' for k, v in tool_args.items() if v is not None)
    return f"{tool_name}({args_str})"
