"""Tests for assistant models."""
import pytest
from assistant.models import AssistantConversation, AssistantActionLog


@pytest.mark.django_db
class TestAssistantConversation:

    def test_create_conversation(self, admin_user):
        conv = AssistantConversation.objects.create(
            user=admin_user,
            context='general',
        )
        assert conv.id is not None
        assert conv.message_count == 0
        assert conv.title == ''
        assert conv.summary == ''
        assert conv.first_message == ''

    def test_conversation_ordering(self, admin_user):
        """Conversations ordered by -updated_at (most recent first)."""
        c1 = AssistantConversation.objects.create(user=admin_user, context='general')
        c2 = AssistantConversation.objects.create(user=admin_user, context='general')

        convs = list(AssistantConversation.objects.filter(user=admin_user))
        assert convs[0].id == c2.id

    def test_conversation_str(self, admin_user):
        conv = AssistantConversation.objects.create(
            user=admin_user, context='setup',
        )
        s = str(conv)
        assert 'setup' in s
        assert admin_user.name in s


@pytest.mark.django_db
class TestAssistantActionLog:

    def test_create_action_log(self, admin_user, conversation):
        log = AssistantActionLog.objects.create(
            user=admin_user,
            conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={'success': True},
            success=True,
            confirmed=True,
        )
        assert log.id is not None
        assert log.tool_name == 'create_product'
        assert log.success is True
        assert log.confirmed is True

    def test_action_log_str(self, admin_user, conversation):
        log = AssistantActionLog.objects.create(
            user=admin_user,
            conversation=conversation,
            tool_name='create_product',
            tool_args={},
            success=False,
            confirmed=False,
        )
        s = str(log)
        assert 'create_product' in s
        assert 'pending' in s

        log.confirmed = True
        s2 = str(log)
        assert 'confirmed' in s2

    def test_action_log_ordering(self, admin_user, conversation):
        """Action logs ordered by -created_at."""
        l1 = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='tool_a', tool_args={},
        )
        l2 = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='tool_b', tool_args={},
        )

        logs = list(AssistantActionLog.objects.all())
        assert logs[0].tool_name == 'tool_b'

    def test_action_log_cascade_conversation_delete(self, admin_user, conversation):
        """Setting conversation to NULL when conversation deleted."""
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='test', tool_args={},
        )
        conversation.delete()
        # Log should still exist with conversation=None (SET_NULL)
        assert AssistantActionLog.objects.filter(tool_name='test').exists()
        log = AssistantActionLog.objects.get(tool_name='test')
        assert log.conversation is None
