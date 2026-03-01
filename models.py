from django.db import models


class AssistantConversation(models.Model):
    """Tracks conversation state per user."""
    user = models.ForeignKey(
        'accounts.LocalUser',
        on_delete=models.CASCADE,
        related_name='assistant_conversations',
    )
    openai_response_id = models.CharField(max_length=255, blank=True, default='')
    context = models.CharField(max_length=50, default='general')
    title = models.CharField(max_length=200, blank=True, default='')
    summary = models.TextField(blank=True, default='')
    first_message = models.TextField(blank=True, default='')
    message_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Conversation {self.id} ({self.user.name}, {self.context})"


class AssistantActionLog(models.Model):
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = 'confirmed' if self.confirmed else 'pending'
        return f"{self.tool_name} ({status}) by {self.user.name}"
