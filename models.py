from django.db import models

from apps.core.models.base import HubBaseModel


class AssistantConversation(HubBaseModel):
    """Tracks conversation state per user."""
    user = models.ForeignKey(
        'accounts.LocalUser',
        on_delete=models.CASCADE,
        related_name='assistant_conversations',
    )
    ai_conversation_id = models.CharField(max_length=255, blank=True, default='')
    context = models.CharField(max_length=50, default='general')
    title = models.CharField(max_length=200, blank=True, default='')
    summary = models.TextField(blank=True, default='')
    first_message = models.TextField(blank=True, default='')
    message_count = models.PositiveIntegerField(default=0)

    class Meta(HubBaseModel.Meta):
        db_table = 'assistant_assistantconversation'
        ordering = ['-updated_at']

    def __str__(self):
        return f"Conversation {self.id} ({self.user.name}, {self.context})"


class AssistantActionLog(HubBaseModel):
    """Audit trail for all assistant-executed actions."""
    user = models.ForeignKey(
        'accounts.LocalUser',
        on_delete=models.CASCADE,
        related_name='assistant_action_logs',
    )
    conversation = models.ForeignKey(
        AssistantConversation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='action_logs',
    )
    tool_name = models.CharField(max_length=100)
    tool_args = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    success = models.BooleanField(default=False)
    confirmed = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default='')
    llm_message_id = models.CharField(max_length=100, blank=True, default='')

    class Meta(HubBaseModel.Meta):
        db_table = 'assistant_assistantactionlog'
        ordering = ['-created_at']

    def __str__(self):
        status = 'confirmed' if self.confirmed else 'pending'
        return f"{self.tool_name} ({status}) by {self.user.name}"


class AssistantFeedback(HubBaseModel):
    """
    Tracks feedback events for product improvement.

    Automatically recorded when tools fail, searches return zero results,
    or users request features that don't exist. Sent to Cloud for
    analysis and email notification to the ERPlora team.
    """
    EVENT_TYPES = [
        ('tool_error', 'Tool Error'),
        ('zero_results', 'Zero Results'),
        ('missing_feature', 'Missing Feature'),
    ]

    event_type = models.CharField(max_length=30, choices=EVENT_TYPES)
    tool_name = models.CharField(max_length=100, blank=True, default='')
    user_message = models.TextField(blank=True, default='')
    details = models.JSONField(default=dict)
    user = models.ForeignKey(
        'accounts.LocalUser',
        on_delete=models.CASCADE,
        related_name='assistant_feedback',
    )
    conversation = models.ForeignKey(
        AssistantConversation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='feedback_events',
    )
    action_log = models.ForeignKey(
        AssistantActionLog,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='feedback_events',
    )
    sent_to_cloud = models.BooleanField(default=False)
    cloud_error = models.CharField(max_length=255, blank=True, default='')

    class Meta(HubBaseModel.Meta):
        db_table = 'assistant_assistantfeedback'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event_type', 'created_at']),
            models.Index(fields=['sent_to_cloud', 'created_at']),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} — {self.tool_name or 'N/A'}"
