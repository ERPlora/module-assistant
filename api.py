"""
AI Assistant REST API.

JSON endpoints for chat, conversations, actions, and logs.
Enables CLI/console interaction with the assistant.
"""
import logging

from rest_framework import status, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiResponse

from apps.accounts.models import LocalUser
from apps.core.api_base import IsAuthenticated, SuccessResponseSerializer, ErrorResponseSerializer

from .models import AssistantConversation, AssistantActionLog
from .views import (
    run_agentic_loop,
    AgenticLoopError,
    execute_confirmed_action,
    _get_or_create_conversation,
)
from .tools import get_tool, get_tools_for_context

logger = logging.getLogger(__name__)


class AssistantAPIView(APIView):
    """Base view for assistant API — disables DRF's default SessionAuthentication CSRF."""
    authentication_classes = []  # Auth handled by IsAuthenticated permission (session check)


# =============================================================================
# Serializers
# =============================================================================

class ChatRequestSerializer(serializers.Serializer):
    """Request for sending a chat message."""
    message = serializers.CharField(help_text="User message text")
    conversation_id = serializers.IntegerField(
        required=False,
        help_text="Existing conversation ID to continue (omit for new conversation)",
    )
    context = serializers.CharField(
        default='general',
        required=False,
        help_text="Context: 'general' or 'setup'",
    )


class PendingActionSerializer(serializers.Serializer):
    """A pending action awaiting user confirmation."""
    log_id = serializers.IntegerField(help_text="Action log ID (use to confirm/cancel)")
    tool_name = serializers.CharField(help_text="Tool that will be executed")
    tool_args = serializers.DictField(help_text="Arguments that will be passed to the tool")
    description = serializers.CharField(help_text="Human-readable description of the action")


class TierInfoSerializer(serializers.Serializer):
    """AI tier and usage information."""
    tier = serializers.CharField(required=False)
    sessions_used = serializers.IntegerField(required=False)
    sessions_limit = serializers.IntegerField(required=False)
    tier_name = serializers.CharField(required=False)


class ChatResponseSerializer(serializers.Serializer):
    """Response from the chat endpoint."""
    conversation_id = serializers.IntegerField(help_text="Conversation ID")
    response_text = serializers.CharField(
        help_text="Assistant's text response (markdown)",
        allow_blank=True,
    )
    pending_actions = PendingActionSerializer(many=True, help_text="Actions awaiting confirmation")
    tier_info = TierInfoSerializer(required=False, allow_null=True)


class ConversationListSerializer(serializers.ModelSerializer):
    """Serializer for listing conversations."""
    class Meta:
        model = AssistantConversation
        fields = ['id', 'context', 'created_at', 'updated_at']
        read_only_fields = fields


class ActionLogSerializer(serializers.ModelSerializer):
    """Serializer for action logs."""

    class Meta:
        model = AssistantActionLog
        fields = [
            'id', 'conversation_id', 'tool_name', 'tool_args',
            'result', 'success', 'confirmed', 'error_message', 'created_at',
        ]
        read_only_fields = fields


class ActionConfirmResponseSerializer(serializers.Serializer):
    """Response from confirming an action."""
    success = serializers.BooleanField()
    message = serializers.CharField()
    result = serializers.DictField(required=False)


# =============================================================================
# API Views
# =============================================================================

@extend_schema(tags=['Assistant'])
class ChatView(AssistantAPIView):
    """
    Send a message to the AI assistant.

    The assistant processes the message through an agentic loop:
    1. Builds context (hub config, modules, user info)
    2. Calls the AI model via Cloud proxy
    3. Executes read tools automatically
    4. Returns write tools as pending actions for confirmation

    Use the conversation_id to continue an existing conversation thread.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Chat with assistant",
        description="Send a message and get AI response with optional pending actions",
        request=ChatRequestSerializer,
        responses={
            200: ChatResponseSerializer,
            400: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = serializer.validated_data['message']
        conversation_id = serializer.validated_data.get('conversation_id')
        context = serializer.validated_data.get('context', 'general')

        user_id = request.session.get('local_user_id')
        if not user_id:
            return Response(
                {'success': False, 'error': 'Not authenticated'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            user = LocalUser.objects.get(id=user_id)
        except LocalUser.DoesNotExist:
            return Response(
                {'success': False, 'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        conversation = _get_or_create_conversation(user_id, conversation_id, context)

        try:
            result = run_agentic_loop(user, conversation, message, context, request)
        except AgenticLoopError as e:
            return Response(
                {'success': False, 'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            'conversation_id': result['conversation_id'],
            'response_text': result['response_text'],
            'pending_actions': result['pending_actions'],
            'tier_info': result['tier_info'],
        })


@extend_schema(tags=['Assistant'])
class ConversationListView(AssistantAPIView):
    """List the current user's conversations."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List conversations",
        description="Get recent conversations for the authenticated user",
        responses={200: ConversationListSerializer(many=True)},
    )
    def get(self, request):
        user_id = request.session.get('local_user_id')
        conversations = AssistantConversation.objects.filter(
            user_id=user_id,
        ).order_by('-updated_at')[:50]

        serializer = ConversationListSerializer(conversations, many=True)
        return Response(serializer.data)


@extend_schema(tags=['Assistant'])
class ConversationDetailView(AssistantAPIView):
    """Get details of a specific conversation, including its action logs."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get conversation details",
        description="Get conversation info and its action log history",
        responses={
            200: OpenApiResponse(description="Conversation with action logs"),
            404: ErrorResponseSerializer,
        },
    )
    def get(self, request, pk):
        user_id = request.session.get('local_user_id')
        try:
            conversation = AssistantConversation.objects.get(id=pk, user_id=user_id)
        except AssistantConversation.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Conversation not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        logs = AssistantActionLog.objects.filter(
            conversation=conversation,
        ).order_by('-created_at')

        return Response({
            'id': conversation.id,
            'context': conversation.context,
            'created_at': conversation.created_at,
            'updated_at': conversation.updated_at,
            'action_logs': ActionLogSerializer(logs, many=True).data,
        })


@extend_schema(tags=['Assistant'])
class ActionConfirmView(AssistantAPIView):
    """Confirm and execute a pending action."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Confirm pending action",
        description="Execute a pending write action after user confirmation",
        responses={
            200: ActionConfirmResponseSerializer,
            404: ErrorResponseSerializer,
        },
    )
    def post(self, request, pk):
        user_id = request.session.get('local_user_id')

        try:
            action_log = AssistantActionLog.objects.get(
                id=pk,
                user_id=user_id,
                confirmed=False,
            )
        except AssistantActionLog.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Action not found or already processed'},
                status=status.HTTP_404_NOT_FOUND,
            )

        result = execute_confirmed_action(action_log, request)
        return Response(result)


@extend_schema(tags=['Assistant'])
class ActionCancelView(AssistantAPIView):
    """Cancel a pending action."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Cancel pending action",
        description="Cancel a pending write action",
        responses={200: SuccessResponseSerializer, 404: ErrorResponseSerializer},
    )
    def post(self, request, pk):
        user_id = request.session.get('local_user_id')

        try:
            action_log = AssistantActionLog.objects.get(
                id=pk,
                user_id=user_id,
                confirmed=False,
            )
            action_log.delete()
        except AssistantActionLog.DoesNotExist:
            return Response(
                {'success': False, 'error': 'Action not found or already processed'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({'success': True, 'message': 'Action cancelled'})


@extend_schema(tags=['Assistant'])
class ActionLogListView(AssistantAPIView):
    """List action logs for the current user."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List action logs",
        description="Get recent action logs with tool execution results",
        responses={200: ActionLogSerializer(many=True)},
    )
    def get(self, request):
        user_id = request.session.get('local_user_id')
        logs = AssistantActionLog.objects.filter(
            user_id=user_id,
        ).select_related('conversation').order_by('-created_at')[:100]

        serializer = ActionLogSerializer(logs, many=True)
        return Response(serializer.data)


# =============================================================================
# Tool Execution API (direct tool calls without Cloud LLM)
# =============================================================================

class ExecuteToolRequestSerializer(serializers.Serializer):
    """Request for executing a tool directly."""
    tool_name = serializers.CharField(help_text="Tool name (e.g., 'configure_business', 'execute_plan')")
    tool_args = serializers.DictField(help_text="Arguments to pass to the tool")
    context = serializers.CharField(
        default='general',
        required=False,
        help_text="Context: 'general' or 'setup'",
    )


class ExecuteToolResponseSerializer(serializers.Serializer):
    """Response from executing a tool."""
    success = serializers.BooleanField()
    tool_name = serializers.CharField()
    requires_confirmation = serializers.BooleanField()
    result = serializers.DictField(required=False, allow_null=True)
    pending_action = PendingActionSerializer(required=False, allow_null=True)
    error = serializers.CharField(required=False)


class ListToolsResponseSerializer(serializers.Serializer):
    """Response listing available tools."""
    tools = serializers.ListField()
    total = serializers.IntegerField()


@extend_schema(tags=['Assistant'])
class ExecuteToolView(AssistantAPIView):
    """
    Execute a tool directly without going through the LLM.

    This endpoint allows direct tool execution, useful for:
    - Testing tool behavior
    - CLI/console automation
    - Scripted configurations

    Tools with requires_confirmation=True will be stored as pending
    actions and must be confirmed via /actions/<id>/confirm/.
    Tools with requires_confirmation=False execute immediately.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Execute a tool directly",
        description="Execute an assistant tool without LLM, returns result or pending action",
        request=ExecuteToolRequestSerializer,
        responses={
            200: ExecuteToolResponseSerializer,
            400: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
    )
    def post(self, request):
        serializer = ExecuteToolRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tool_name = serializer.validated_data['tool_name']
        tool_args = serializer.validated_data['tool_args']
        context = serializer.validated_data.get('context', 'general')

        user_id = request.session.get('local_user_id')
        if not user_id:
            return Response(
                {'success': False, 'error': 'Not authenticated'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            user = LocalUser.objects.get(id=user_id)
        except LocalUser.DoesNotExist:
            return Response(
                {'success': False, 'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        tool = get_tool(tool_name)
        if not tool:
            return Response(
                {'success': False, 'error': f'Tool not found: {tool_name}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Setup-only check
        if tool.setup_only and context != 'setup':
            return Response(
                {'success': False, 'error': f'Tool {tool_name} is only available in setup context'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Permission check
        if tool.required_permission and not user.has_perm(tool.required_permission):
            return Response(
                {'success': False, 'error': f'Permission denied: {tool.required_permission}'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # If tool requires confirmation, create pending action
        if tool.requires_confirmation:
            conversation = _get_or_create_conversation(user_id, None, context)
            action_log = AssistantActionLog.objects.create(
                user_id=user_id,
                conversation=conversation,
                tool_name=tool_name,
                tool_args=tool_args,
                confirmed=False,
            )
            return Response({
                'success': True,
                'tool_name': tool_name,
                'requires_confirmation': True,
                'result': None,
                'pending_action': {
                    'log_id': str(action_log.id),
                    'tool_name': tool_name,
                    'tool_args': tool_args,
                    'description': f'Execute {tool_name}',
                },
            })

        # Execute immediately (read-only tools)
        try:
            result = tool.execute(tool_args, request)
            return Response({
                'success': True,
                'tool_name': tool_name,
                'requires_confirmation': False,
                'result': result,
                'pending_action': None,
            })
        except Exception as e:
            logger.error(f"[ASSISTANT API] Tool {tool_name} failed: {e}", exc_info=True)
            return Response(
                {'success': False, 'error': f'Tool execution failed: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )


@extend_schema(tags=['Assistant'])
class ListToolsView(AssistantAPIView):
    """List all available tools for the current user and context."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List available tools",
        description="Get all tools available for the current user",
        responses={200: ListToolsResponseSerializer},
    )
    def get(self, request):
        user_id = request.session.get('local_user_id')
        context = request.query_params.get('context', 'general')

        user = None
        if user_id:
            try:
                user = LocalUser.objects.get(id=user_id)
            except LocalUser.DoesNotExist:
                pass

        tools = get_tools_for_context(context, user)
        return Response({
            'tools': tools,
            'total': len(tools),
        })


# =============================================================================
# URL Patterns
# =============================================================================

from django.urls import path

api_urlpatterns = [
    path('chat/', ChatView.as_view(), name='api_assistant_chat'),
    path('conversations/', ConversationListView.as_view(), name='api_assistant_conversations'),
    path('conversations/<int:pk>/', ConversationDetailView.as_view(), name='api_assistant_conversation_detail'),
    path('actions/<int:pk>/confirm/', ActionConfirmView.as_view(), name='api_assistant_action_confirm'),
    path('actions/<int:pk>/cancel/', ActionCancelView.as_view(), name='api_assistant_action_cancel'),
    path('logs/', ActionLogListView.as_view(), name='api_assistant_logs'),
    path('tools/', ListToolsView.as_view(), name='api_assistant_tools'),
    path('tools/execute/', ExecuteToolView.as_view(), name='api_assistant_execute_tool'),
]
