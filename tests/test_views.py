"""Tests for assistant views."""
import json
import pytest
from unittest.mock import patch, MagicMock
from django.test import Client
from django.core.cache import cache

from assistant.models import AssistantConversation, AssistantActionLog
from assistant.views import (
    format_confirmation_text,
    _get_or_create_conversation,
    _track_conversation_message,
    _summarize_last_conversation,
    execute_confirmed_action,
)


class TestFormatConfirmationText:
    """Tests for format_confirmation_text()."""

    def test_known_tool(self):
        text = format_confirmation_text('create_product', {'name': 'Champú', 'price': '12.50'})
        assert 'Champú' in text
        assert '12.50' in text

    def test_create_customer(self):
        text = format_confirmation_text('create_customer', {'name': 'María García'})
        assert 'María García' in text

    def test_execute_plan(self):
        text = format_confirmation_text('execute_plan', {'steps': [1, 2, 3]})
        assert '3 steps' in text

    def test_toggle_sync(self):
        text = format_confirmation_text('toggle_marketplace_sync', {'connection_id': '1', 'enabled': True})
        assert 'Enable' in text

    def test_toggle_sync_disable(self):
        text = format_confirmation_text('toggle_marketplace_sync', {'connection_id': '1', 'enabled': False})
        assert 'Disable' in text

    def test_create_sii_submission(self):
        text = format_confirmation_text('create_sii_submission', {
            'submission_type': 'issued_invoices', 'period': '2026-Q1',
        })
        assert 'issued_invoices' in text
        assert '2026-Q1' in text

    def test_unknown_tool_fallback(self):
        text = format_confirmation_text('some_unknown_tool', {'foo': 'bar', 'baz': 42})
        assert 'some_unknown_tool' in text
        assert 'foo=bar' in text

    def test_formatter_exception_uses_fallback(self):
        """If the formatter lambda raises, falls back to generic."""
        text = format_confirmation_text('execute_plan', {})  # missing 'steps' key
        # Should not crash — either the lambda handles it or fallback
        assert isinstance(text, str)
        assert len(text) > 0

    def test_all_common_tools_have_formatters(self):
        """Check that commonly used tools have specific formatters."""
        common_tools = [
            'create_product', 'create_customer', 'create_service',
            'create_appointment', 'create_expense', 'create_project',
            'create_ticket', 'create_invoice', 'create_lead',
            'create_quote', 'adjust_stock', 'create_role',
            'create_employee', 'update_store_config',
        ]
        for tool_name in common_tools:
            text = format_confirmation_text(tool_name, {'name': 'X', 'price': '1'})
            # Should NOT be the generic fallback format
            assert f'{tool_name}(' not in text, f"{tool_name} is using generic fallback"


@pytest.mark.django_db
class TestConversationMemory:
    """Tests for conversation memory helpers."""

    def test_track_first_message(self, conversation):
        _track_conversation_message(conversation, 'Hola, tengo una peluquería')
        conversation.refresh_from_db()
        assert conversation.message_count == 1
        assert conversation.first_message == 'Hola, tengo una peluquería'
        assert conversation.title == 'Hola, tengo una peluquería'

    def test_track_second_message_no_title_change(self, conversation):
        _track_conversation_message(conversation, 'First message')
        _track_conversation_message(conversation, 'Second message')
        conversation.refresh_from_db()
        assert conversation.message_count == 2
        assert conversation.title == 'First message'

    def test_track_long_message_truncated(self, conversation):
        long_msg = 'x' * 600
        _track_conversation_message(conversation, long_msg)
        conversation.refresh_from_db()
        assert len(conversation.first_message) == 500
        assert len(conversation.title) == 100

    def test_summarize_conversation(self, admin_user, conversation):
        """_summarize_last_conversation builds summary from action logs."""
        # Create some action logs
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='list_products', tool_args={},
            success=True, confirmed=True,
        )
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product', tool_args={'name': 'Champú'},
            success=True, confirmed=True,
        )
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_service', tool_args={'name': 'Corte'},
            success=True, confirmed=True,
        )

        _summarize_last_conversation(str(admin_user.id))
        conversation.refresh_from_db()
        # list_products should be skipped (it's a discovery tool)
        assert 'list_products' not in conversation.summary
        assert "create_product 'Champú'" in conversation.summary
        assert "create_service 'Corte'" in conversation.summary

    def test_get_or_create_conversation_existing(self, admin_user, conversation):
        """Returns existing conversation when valid ID provided."""
        result = _get_or_create_conversation(
            str(admin_user.id), str(conversation.id), 'general',
        )
        assert result.id == conversation.id

    def test_get_or_create_conversation_new(self, admin_user):
        """Creates new conversation when no ID provided."""
        result = _get_or_create_conversation(
            str(admin_user.id), '', 'general',
        )
        assert result.id is not None
        assert result.context == 'general'

    def test_get_or_create_conversation_invalid_id(self, admin_user):
        """Creates new conversation when invalid ID provided."""
        result = _get_or_create_conversation(
            str(admin_user.id), '99999', 'general',
        )
        assert result.id is not None


@pytest.mark.django_db
class TestExecuteConfirmedAction:
    """Tests for execute_confirmed_action()."""

    def test_execute_success(self, action_log, request_with_session):
        """Successful execution updates action log."""
        with patch('assistant.views.get_tool') as mock_get:
            mock_tool = MagicMock()
            mock_tool.safe_execute.return_value = {'success': True, 'id': '123'}
            mock_get.return_value = mock_tool

            result = execute_confirmed_action(action_log, request_with_session)
            assert result['success'] is True
            assert result['message'] == 'Action confirmed and executed successfully.'

            action_log.refresh_from_db()
            assert action_log.success is True
            assert action_log.confirmed is True

    def test_execute_tool_not_found(self, action_log, request_with_session):
        """Returns error when tool not found."""
        action_log.tool_name = 'nonexistent_tool'
        action_log.save()

        with patch('assistant.views.get_tool', return_value=None):
            result = execute_confirmed_action(action_log, request_with_session)
            assert result['success'] is False
            assert 'not found' in result['message']

    def test_execute_safe_error_dict(self, action_log, request_with_session):
        """safe_execute returning error dict is handled as failure."""
        with patch('assistant.views.get_tool') as mock_get:
            mock_tool = MagicMock()
            mock_tool.safe_execute.return_value = {'error': 'Product not found (id: abc)'}
            mock_get.return_value = mock_tool

            result = execute_confirmed_action(action_log, request_with_session)
            assert result['success'] is False
            assert 'not found' in result['message']

            action_log.refresh_from_db()
            assert action_log.success is False
            assert action_log.confirmed is True

    def test_execute_exception(self, action_log, request_with_session):
        """Exception during execution is caught and logged."""
        with patch('assistant.views.get_tool') as mock_get:
            mock_tool = MagicMock()
            mock_tool.safe_execute.side_effect = RuntimeError('Database error')
            mock_get.return_value = mock_tool

            result = execute_confirmed_action(action_log, request_with_session)
            assert result['success'] is False
            assert 'Database error' in result['message']
