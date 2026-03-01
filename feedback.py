"""
Automatic feedback service for the AI Assistant.

Detects tool errors, zero-result searches, and missing feature requests.
Records them locally and sends to Cloud for team notification.
"""
import logging
import threading

import requests as http_requests
from django.utils import timezone

logger = logging.getLogger(__name__)

# Deduplication window in seconds
DEDUP_WINDOW = 300  # 5 minutes


def record_feedback(
    event_type,
    user,
    conversation=None,
    action_log=None,
    tool_name='',
    user_message='',
    details=None,
):
    """
    Record a feedback event and send to Cloud in a background thread.

    Args:
        event_type: 'tool_error', 'zero_results', or 'missing_feature'
        user: LocalUser instance
        conversation: AssistantConversation (optional)
        action_log: AssistantActionLog (optional)
        tool_name: Name of the tool that triggered the event
        user_message: The user's original message
        details: dict with extra context (error message, query, etc.)
    """
    from .models import AssistantFeedback

    if _is_duplicate(event_type, tool_name, user):
        logger.debug(
            f"[FEEDBACK] Skipping duplicate: {event_type}/{tool_name} for user {user.id}"
        )
        return None

    feedback = AssistantFeedback.objects.create(
        event_type=event_type,
        tool_name=tool_name,
        user_message=user_message[:2000] if user_message else '',
        details=details or {},
        user=user,
        conversation=conversation,
        action_log=action_log,
    )

    # Fire-and-forget: send to Cloud in a daemon thread
    thread = threading.Thread(
        target=_send_to_cloud_safe,
        args=(feedback.id,),
        daemon=True,
    )
    thread.start()

    return feedback


def _is_duplicate(event_type, tool_name, user):
    """
    Check if the same event was already recorded within the dedup window.

    Prevents flooding Cloud with repeated errors (e.g., user retrying
    the same failing tool).
    """
    from .models import AssistantFeedback

    cutoff = timezone.now() - timezone.timedelta(seconds=DEDUP_WINDOW)
    return AssistantFeedback.objects.filter(
        event_type=event_type,
        tool_name=tool_name,
        user=user,
        created_at__gte=cutoff,
    ).exists()


def _send_to_cloud_safe(feedback_id):
    """Wrapper that catches all exceptions so the thread never crashes."""
    try:
        send_feedback_to_cloud(feedback_id)
    except Exception as e:
        logger.warning(f"[FEEDBACK] Cloud send failed (id={feedback_id}): {e}")


def send_feedback_to_cloud(feedback_id):
    """
    POST feedback event to Cloud API.

    Endpoint: /api/hubs/me/assistant/feedback/
    Auth: Hub JWT token
    Gracefully handles 404 (endpoint not deployed yet), timeouts, errors.
    """
    from apps.configuration.models import HubConfig
    from .models import AssistantFeedback

    try:
        feedback = AssistantFeedback.objects.get(id=feedback_id)
    except AssistantFeedback.DoesNotExist:
        return

    config = HubConfig.get_solo()
    if not config.hub_jwt:
        feedback.cloud_error = 'No hub_jwt configured'
        feedback.save(update_fields=['cloud_error'])
        return

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com')

    payload = {
        'event_type': feedback.event_type,
        'tool_name': feedback.tool_name,
        'user_message': feedback.user_message,
        'details': feedback.details,
        'created_at': feedback.created_at.isoformat(),
    }

    try:
        response = http_requests.post(
            f"{base_url}/api/hubs/me/assistant/feedback/",
            json=payload,
            headers={
                'Authorization': f'Bearer {config.hub_jwt}',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )

        if response.status_code in (200, 201):
            feedback.sent_to_cloud = True
            feedback.save(update_fields=['sent_to_cloud'])
        else:
            feedback.cloud_error = f"HTTP {response.status_code}"
            feedback.save(update_fields=['cloud_error'])

    except http_requests.ConnectionError:
        feedback.cloud_error = 'Connection error'
        feedback.save(update_fields=['cloud_error'])
    except http_requests.Timeout:
        feedback.cloud_error = 'Timeout'
        feedback.save(update_fields=['cloud_error'])
    except Exception as e:
        feedback.cloud_error = str(e)[:255]
        feedback.save(update_fields=['cloud_error'])
