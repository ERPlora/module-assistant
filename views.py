"""
AI Assistant Views.

Handles chat page rendering, message processing with agentic loop,
and action confirmation.
"""
import json
import logging

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.accounts.decorators import login_required, permission_required
from apps.modules_runtime.navigation import with_module_nav
from apps.core.htmx import htmx_view

from .models import AssistantConversation, AssistantActionLog
from .prompts import build_system_prompt
from .tools import get_tools_for_context, get_tool

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10


# ============================================================================
# PAGE VIEWS
# ============================================================================

@login_required
@permission_required('assistant.use_chat')
@with_module_nav('assistant', 'chat')
@htmx_view('assistant/pages/chat.html', 'assistant/partials/chat_panel.html')
def chat_page(request):
    """Main chat page."""
    conversations = AssistantConversation.objects.filter(
        user_id=request.session.get('local_user_id'),
    ).order_by('-updated_at')[:10]

    return {
        'conversations': conversations,
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
# CHAT API
# ============================================================================

@login_required
@permission_required('assistant.use_chat')
@require_POST
def chat(request):
    """
    Process a chat message through the agentic loop.

    1. Get or create conversation
    2. Build system prompt
    3. Call Cloud proxy → OpenAI
    4. Execute tool calls locally
    5. Loop until no more tool calls or confirmation needed
    6. Return HTML partial with assistant response
    """
    message = request.POST.get('message', '').strip()
    conversation_id = request.POST.get('conversation_id', '')
    context = request.POST.get('context', 'general')

    if not message:
        return HttpResponse(
            render_to_string('assistant/partials/message.html', {
                'role': 'assistant',
                'content': 'Please type a message.',
            }, request=request),
        )

    user_id = request.session.get('local_user_id')

    # Get or create conversation
    conversation = _get_or_create_conversation(user_id, conversation_id, context)

    # Get user object for permission checks
    from apps.accounts.models import LocalUser
    try:
        user = LocalUser.objects.get(id=user_id)
    except LocalUser.DoesNotExist:
        return _error_response("User not found", request)

    # Build system prompt and tools
    instructions = build_system_prompt(request, context)
    tools = get_tools_for_context(context, user)

    # Build input for OpenAI
    openai_input = message

    # If we have a previous response_id, OpenAI will continue that conversation
    previous_response_id = conversation.openai_response_id or None

    # Determine if this is a new session (no previous conversation thread)
    is_new_session = not conversation.openai_response_id

    # Agentic loop
    response_text = ""
    pending_confirmation = None
    tier_info = None

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response_data, loop_tier_info = _call_cloud_proxy(
                request=request,
                input_data=openai_input,
                instructions=instructions,
                tools=tools,
                previous_response_id=previous_response_id,
                new_session=(is_new_session and iteration == 0),
            )
            if loop_tier_info:
                tier_info = loop_tier_info
        except CloudProxyError as e:
            logger.error(f"[ASSISTANT] Cloud proxy error: {e}")
            if e.status_code == 403:
                return _error_response(
                    "AI Assistant subscription required. Please subscribe via the marketplace.",
                    request,
                )
            if e.status_code == 429:
                limit = e.error_data.get('limit', '')
                used = e.error_data.get('used', '')
                return _error_response(
                    f"Monthly usage limit reached ({used}/{limit} sessions). "
                    "Please upgrade your plan or wait until next month.",
                    request,
                )
            return _error_response(f"Error connecting to AI service: {str(e)}", request)
        except Exception as e:
            logger.error(f"[ASSISTANT] Cloud proxy error: {e}")
            return _error_response(f"Error connecting to AI service: {str(e)}", request)

        if not response_data:
            return _error_response("No response from AI service", request)

        # Save response ID for conversation threading
        response_id = response_data.get('id', '')
        if response_id:
            conversation.openai_response_id = response_id
            conversation.save(update_fields=['openai_response_id', 'updated_at'])

        # Extract output items
        output = response_data.get('output', [])

        # Collect text and function calls
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
        for fc in function_calls:
            tool_name = fc.get('name', '')
            call_id = fc.get('call_id', '')
            try:
                tool_args = json.loads(fc.get('arguments', '{}'))
            except json.JSONDecodeError:
                tool_args = {}

            tool = get_tool(tool_name)
            if not tool:
                tool_results.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': json.dumps({"error": f"Unknown tool: {tool_name}"}),
                })
                continue

            # Permission check
            if tool.required_permission and not user.has_perm(tool.required_permission):
                tool_results.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': json.dumps({"error": f"Permission denied: {tool.required_permission}"}),
                })
                continue

            # Confirmation check
            if tool.requires_confirmation:
                # Create pending action log
                action_log = AssistantActionLog.objects.create(
                    user_id=user_id,
                    conversation=conversation,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    result={},
                    success=False,
                    confirmed=False,
                )
                pending_confirmation = {
                    'log_id': action_log.id,
                    'tool_name': tool_name,
                    'tool_args': tool_args,
                    'tool_description': tool.description,
                }
                # Tell the LLM the action is pending confirmation
                tool_results.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': json.dumps({
                        "status": "pending_confirmation",
                        "message": f"Action '{tool_name}' requires user confirmation before execution.",
                        "action_id": action_log.id,
                    }),
                })
                # Don't execute more tools — wait for confirmation
                break
            else:
                # Execute immediately (read tools)
                try:
                    result = tool.execute(tool_args, request)
                    AssistantActionLog.objects.create(
                        user_id=user_id,
                        conversation=conversation,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        result=result,
                        success=True,
                        confirmed=True,
                    )
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': json.dumps(result),
                    })
                except Exception as e:
                    logger.error(f"[ASSISTANT] Tool {tool_name} error: {e}", exc_info=True)
                    AssistantActionLog.objects.create(
                        user_id=user_id,
                        conversation=conversation,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        result={"error": str(e)},
                        success=False,
                        confirmed=True,
                        error_message=str(e),
                    )
                    tool_results.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': json.dumps({"error": str(e)}),
                    })

        # If we have a pending confirmation, break and let the LLM describe it
        if pending_confirmation:
            # Send tool results back to get the LLM's description of the pending action
            openai_input = tool_results
            previous_response_id = response_id
            # One more iteration to get the description
            continue

        # If we only have tool results (no pending), send them back for next iteration
        if tool_results:
            openai_input = tool_results
            previous_response_id = response_id
        else:
            break

    # Build response HTML
    html_parts = []

    if response_text:
        html_parts.append(render_to_string('assistant/partials/message.html', {
            'role': 'assistant',
            'content': response_text,
        }, request=request))

    if pending_confirmation:
        html_parts.append(render_to_string('assistant/partials/confirmation.html', {
            'log_id': pending_confirmation['log_id'],
            'tool_name': pending_confirmation['tool_name'],
            'tool_args': pending_confirmation['tool_args'],
            'description': _format_confirmation_text(
                pending_confirmation['tool_name'],
                pending_confirmation['tool_args'],
            ),
        }, request=request))

    response = HttpResponse(''.join(html_parts))
    response['X-Conversation-Id'] = str(conversation.id)
    if tier_info:
        response['X-Assistant-Tier'] = tier_info.get('tier', '')
        response['X-Assistant-Usage'] = json.dumps({
            'sessions_used': tier_info.get('sessions_used', 0),
            'sessions_limit': tier_info.get('sessions_limit', 0),
            'tier_name': tier_info.get('tier_name', ''),
        })
    return response


# ============================================================================
# CONFIRMATION ACTIONS
# ============================================================================

@login_required
@permission_required('assistant.use_chat')
@require_POST
def confirm_action(request, log_id):
    """Confirm and execute a pending write action."""
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

    tool = get_tool(action_log.tool_name)
    if not tool:
        action_log.error_message = f"Tool {action_log.tool_name} not found"
        action_log.save()
        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': f'Error: Tool {action_log.tool_name} not found.',
        }, request=request))

    try:
        result = tool.execute(action_log.tool_args, request)
        action_log.result = result
        action_log.success = True
        action_log.confirmed = True
        action_log.save()

        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': f'Action confirmed and executed successfully.',
            'success': True,
        }, request=request))

    except Exception as e:
        logger.error(f"[ASSISTANT] Confirm action error: {e}", exc_info=True)
        action_log.result = {"error": str(e)}
        action_log.success = False
        action_log.confirmed = True
        action_log.error_message = str(e)
        action_log.save()

        return HttpResponse(render_to_string('assistant/partials/message.html', {
            'role': 'system',
            'content': f'Error executing action: {str(e)}',
            'error': True,
        }, request=request))


@login_required
@permission_required('assistant.use_chat')
@require_POST
def cancel_action(request, log_id):
    """Cancel a pending write action."""
    user_id = request.session.get('local_user_id')

    try:
        action_log = AssistantActionLog.objects.get(
            id=log_id,
            user_id=user_id,
            confirmed=False,
        )
        action_log.delete()
    except AssistantActionLog.DoesNotExist:
        pass

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

    return AssistantConversation.objects.create(
        user_id=user_id,
        context=context,
    )


class CloudProxyError(Exception):
    """Custom exception for Cloud proxy errors with status code."""
    def __init__(self, message, status_code=None, error_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_data = error_data or {}


def _call_cloud_proxy(request, input_data, instructions, tools,
                      previous_response_id=None, new_session=False):
    """
    Call the Cloud proxy endpoint to forward to OpenAI.

    Uses the Hub's JWT token for authentication.
    Cloud determines the model based on the Hub's tier.

    Returns (response_data, tier_info) where tier_info is a dict with
    tier/usage data from response headers (or None).
    """
    import requests as http_requests
    from apps.configuration.models import HubConfig

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        raise CloudProxyError(
            "Hub is not connected to Cloud. Please configure Cloud connection first."
        )

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com')

    payload = {
        'input': input_data,
        'instructions': instructions,
    }

    if tools:
        payload['tools'] = tools

    if previous_response_id:
        payload['previous_response_id'] = previous_response_id

    if new_session:
        payload['new_session'] = True

    response = http_requests.post(
        f"{base_url}/api/hubs/me/assistant/chat/",
        json=payload,
        headers={
            'Authorization': f'Bearer {config.hub_jwt}',
            'Content-Type': 'application/json',
        },
        timeout=60,
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


def _format_confirmation_text(tool_name, tool_args):
    """Format a human-readable description of the pending action."""
    descriptions = {
        # Hub core tools
        'update_store_config': lambda args: f"Update store: {', '.join(k for k, v in args.items() if v is not None)}",
        'select_blocks': lambda args: f"Select blocks: {', '.join(args.get('block_slugs', []))}",
        'enable_module': lambda args: f"Enable module: {args.get('module_id', '')}",
        'disable_module': lambda args: f"Disable module: {args.get('module_id', '')}",
        'create_role': lambda args: f"Create role: {args.get('display_name', args.get('name', ''))}",
        'create_employee': lambda args: f"Create employee: {args.get('name', '')} ({args.get('role_name', '')})",
        'create_tax_class': lambda args: f"Create tax: {args.get('name', '')} ({args.get('rate', '')}%)",
        'set_regional_config': lambda args: f"Set region: {', '.join(f'{k}={v}' for k, v in args.items() if v is not None)}",
        'set_business_info': lambda args: f"Set business: {args.get('business_name', '')}",
        'set_tax_config': lambda args: f"Set tax: {args.get('tax_rate', '')}% (included: {args.get('tax_included', '')})",
        'complete_setup_step': lambda args: "Complete hub setup",
        # Inventory
        'create_product': lambda args: f"Create product: {args.get('name', '')} ({args.get('price', '')})",
        'update_product': lambda args: f"Update product: {args.get('product_id', '')}",
        'create_category': lambda args: f"Create category: {args.get('name', '')}",
        'adjust_stock': lambda args: f"Adjust stock: {args.get('quantity', '')} units for product {args.get('product_id', '')}",
        # Customers
        'create_customer': lambda args: f"Create customer: {args.get('name', '')}",
        'update_customer': lambda args: f"Update customer: {args.get('customer_id', '')}",
        # Services
        'create_service': lambda args: f"Create service: {args.get('name', '')} ({args.get('price', '')})",
        # Quotes
        'create_quote': lambda args: f"Create quote: {args.get('title', '')}",
        # Leads
        'create_lead': lambda args: f"Create lead: {args.get('name', '')} ({args.get('company', '')})",
        'move_lead_stage': lambda args: f"Move lead {args.get('lead_id', '')} to stage {args.get('stage_id', '')}",
        # Purchase Orders
        'create_purchase_order': lambda args: f"Create purchase order for supplier {args.get('supplier_id', '')}",
        # Appointments
        'create_appointment': lambda args: f"Book appointment: {args.get('customer_name', '')} at {args.get('start_datetime', '')}",
        # Expenses
        'create_expense': lambda args: f"Record expense: {args.get('title', '')} ({args.get('amount', '')})",
        # Projects
        'create_project': lambda args: f"Create project: {args.get('name', '')}",
        'log_time_entry': lambda args: f"Log {args.get('hours', '')}h on project {args.get('project_id', '')}",
        # Support
        'create_ticket': lambda args: f"Create ticket: {args.get('subject', '')}",
        # Discounts
        'create_coupon': lambda args: f"Create coupon: {args.get('code', '')} ({args.get('discount_value', '')}{args.get('discount_type', '')})",
        # Loyalty
        'award_loyalty_points': lambda args: f"Award {args.get('points', '')} points to member {args.get('member_id', '')}",
        # Shipping
        'create_shipment': lambda args: f"Create shipment to {args.get('recipient_name', '')}",
        # Gift Cards
        'create_gift_card': lambda args: f"Create gift card: {args.get('initial_balance', '')} value",
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
