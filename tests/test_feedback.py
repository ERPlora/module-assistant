"""Tests for the automatic feedback system."""
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from assistant.models import AssistantFeedback, AssistantActionLog
from assistant.feedback import record_feedback, _is_duplicate, send_feedback_to_cloud


@pytest.mark.django_db
class TestRecordFeedback:
    """Tests for record_feedback()."""

    def test_creates_feedback_model(self, admin_user, conversation):
        """record_feedback creates an AssistantFeedback record."""
        with patch('assistant.feedback.threading'):
            fb = record_feedback(
                event_type='tool_error',
                user=admin_user,
                conversation=conversation,
                tool_name='create_product',
                user_message='Create a shampoo product',
                details={'error': 'Product not found'},
            )
        assert fb is not None
        assert fb.event_type == 'tool_error'
        assert fb.tool_name == 'create_product'
        assert fb.user_message == 'Create a shampoo product'
        assert fb.details == {'error': 'Product not found'}
        assert fb.user == admin_user
        assert fb.conversation == conversation
        assert fb.sent_to_cloud is False

    def test_truncates_long_user_message(self, admin_user):
        """User messages longer than 2000 chars are truncated."""
        long_msg = 'x' * 3000
        with patch('assistant.feedback.threading'):
            fb = record_feedback(
                event_type='missing_feature',
                user=admin_user,
                user_message=long_msg,
            )
        assert len(fb.user_message) == 2000

    def test_links_action_log(self, admin_user, conversation, action_log):
        """record_feedback can link to an action log."""
        with patch('assistant.feedback.threading'):
            fb = record_feedback(
                event_type='tool_error',
                user=admin_user,
                conversation=conversation,
                action_log=action_log,
                tool_name='create_product',
            )
        assert fb.action_log == action_log

    def test_spawns_background_thread(self, admin_user):
        """record_feedback starts a daemon thread for Cloud send."""
        with patch('assistant.feedback.threading') as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread

            record_feedback(
                event_type='tool_error',
                user=admin_user,
                tool_name='test_tool',
            )

            mock_threading.Thread.assert_called_once()
            mock_thread.start.assert_called_once()


@pytest.mark.django_db
class TestDeduplication:
    """Tests for _is_duplicate()."""

    def test_no_duplicate_when_empty(self, admin_user):
        """No duplicate when no previous feedback exists."""
        assert _is_duplicate('tool_error', 'create_product', admin_user) is False

    def test_duplicate_within_window(self, admin_user):
        """Duplicate detected within 5-minute window."""
        AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='create_product',
            user=admin_user,
        )
        assert _is_duplicate('tool_error', 'create_product', admin_user) is True

    def test_skips_duplicate(self, admin_user):
        """record_feedback returns None for duplicates."""
        AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='create_product',
            user=admin_user,
        )
        with patch('assistant.feedback.threading'):
            result = record_feedback(
                event_type='tool_error',
                user=admin_user,
                tool_name='create_product',
            )
        assert result is None
        # Only the original should exist
        assert AssistantFeedback.objects.filter(
            tool_name='create_product',
        ).count() == 1

    def test_different_tool_not_duplicate(self, admin_user):
        """Different tool name is not a duplicate."""
        AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='create_product',
            user=admin_user,
        )
        assert _is_duplicate('tool_error', 'create_service', admin_user) is False

    def test_different_event_type_not_duplicate(self, admin_user):
        """Different event type is not a duplicate."""
        AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='search_across_modules',
            user=admin_user,
        )
        assert _is_duplicate('zero_results', 'search_across_modules', admin_user) is False


@pytest.mark.django_db
class TestSendFeedbackToCloud:
    """Tests for send_feedback_to_cloud()."""

    def test_success(self, admin_user, hub_config):
        """Successful send marks sent_to_cloud=True."""
        fb = AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='create_product',
            user=admin_user,
        )
        mock_response = MagicMock()
        mock_response.status_code = 201
        with patch('assistant.feedback.http_requests.post', return_value=mock_response):
            send_feedback_to_cloud(fb.id)

        fb.refresh_from_db()
        assert fb.sent_to_cloud is True
        assert fb.cloud_error == ''

    def test_404_graceful(self, admin_user, hub_config):
        """Cloud 404 (endpoint not deployed) is handled gracefully."""
        fb = AssistantFeedback.objects.create(
            event_type='missing_feature',
            tool_name='create_reservation',
            user=admin_user,
        )
        mock_response = MagicMock()
        mock_response.status_code = 404
        with patch('assistant.feedback.http_requests.post', return_value=mock_response):
            send_feedback_to_cloud(fb.id)

        fb.refresh_from_db()
        assert fb.sent_to_cloud is False
        assert fb.cloud_error == 'HTTP 404'

    def test_no_jwt(self, admin_user):
        """No hub_jwt configured — stores error, does not crash."""
        from apps.configuration.models import HubConfig
        HubConfig._clear_cache()
        config = HubConfig.get_solo()
        config.hub_jwt = ''
        config.save()

        fb = AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='test',
            user=admin_user,
        )
        send_feedback_to_cloud(fb.id)

        fb.refresh_from_db()
        assert fb.cloud_error == 'No hub_jwt configured'

    def test_connection_error(self, admin_user, hub_config):
        """Connection error handled gracefully."""
        from requests.exceptions import ConnectionError as ConnError

        fb = AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='test',
            user=admin_user,
        )
        with patch('assistant.feedback.http_requests.post', side_effect=ConnError('refused')):
            send_feedback_to_cloud(fb.id)

        fb.refresh_from_db()
        assert fb.cloud_error == 'Connection error'

    def test_timeout_error(self, admin_user, hub_config):
        """Timeout error handled gracefully."""
        from requests.exceptions import Timeout

        fb = AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='test',
            user=admin_user,
        )
        with patch('assistant.feedback.http_requests.post', side_effect=Timeout('timed out')):
            send_feedback_to_cloud(fb.id)

        fb.refresh_from_db()
        assert fb.cloud_error == 'Timeout'

    def test_nonexistent_feedback_id(self, hub_config):
        """Non-existent feedback ID is a no-op."""
        # Should not raise
        send_feedback_to_cloud(99999)


@pytest.mark.django_db
class TestFeedbackModel:
    """Tests for AssistantFeedback model."""

    def test_str_representation(self, admin_user):
        fb = AssistantFeedback.objects.create(
            event_type='tool_error',
            tool_name='create_product',
            user=admin_user,
        )
        s = str(fb)
        assert 'Tool Error' in s
        assert 'create_product' in s

    def test_ordering(self, admin_user):
        """Feedback ordered by -created_at."""
        fb1 = AssistantFeedback.objects.create(
            event_type='tool_error', tool_name='a', user=admin_user,
        )
        fb2 = AssistantFeedback.objects.create(
            event_type='zero_results', tool_name='b', user=admin_user,
        )
        feedbacks = list(AssistantFeedback.objects.all())
        assert feedbacks[0].id == fb2.id

    def test_user_fk_cascade_configured(self):
        """User FK is configured with CASCADE on_delete."""
        field = AssistantFeedback._meta.get_field('user')
        from django.db import models
        assert field.remote_field.on_delete is models.CASCADE

    def test_set_null_conversation(self, admin_user, conversation):
        """Conversation set to NULL when conversation deleted."""
        fb = AssistantFeedback.objects.create(
            event_type='tool_error', tool_name='test',
            user=admin_user, conversation=conversation,
        )
        conversation.delete()
        fb.refresh_from_db()
        assert fb.conversation is None
