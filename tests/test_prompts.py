"""Tests for assistant system prompt builder."""
import pytest
from unittest.mock import patch, MagicMock

from assistant.prompts import build_system_prompt


@pytest.mark.django_db
class TestBuildSystemPrompt:
    """Tests for the system prompt builder."""

    def test_build_prompt_returns_string(self, request_with_session, hub_config, store_config):
        """build_system_prompt returns a non-empty string."""
        prompt = build_system_prompt(request_with_session, 'general')
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_prompt_contains_safety_rules(self, request_with_session, hub_config, store_config):
        """Prompt includes safety rules."""
        prompt = build_system_prompt(request_with_session, 'general')
        assert 'SAFETY' in prompt or 'safety' in prompt or 'never' in prompt.lower()

    def test_prompt_contains_business_info(self, request_with_session, hub_config, store_config):
        """Prompt includes store/business context."""
        prompt = build_system_prompt(request_with_session, 'general')
        assert 'Test Peluquería' in prompt or 'Peluquer' in prompt

    def test_prompt_contains_datetime(self, request_with_session, hub_config, store_config):
        """Prompt includes current date/time."""
        prompt = build_system_prompt(request_with_session, 'general')
        # Should contain a date pattern or timezone
        assert 'Europe/Madrid' in prompt or '2026' in prompt or 'date' in prompt.lower()

    def test_prompt_contains_tools_section(self, request_with_session, hub_config, store_config):
        """Prompt includes available tools."""
        from assistant.tools import discover_tools
        discover_tools()
        prompt = build_system_prompt(request_with_session, 'general')
        assert 'tool' in prompt.lower()

    def test_prompt_setup_context(self, request_with_session, hub_config, store_config):
        """Setup context produces a different prompt."""
        prompt_general = build_system_prompt(request_with_session, 'general')
        prompt_setup = build_system_prompt(request_with_session, 'setup')
        # Setup prompt should include setup-specific content
        assert prompt_general != prompt_setup

    def test_prompt_caching(self, request_with_session, hub_config, store_config):
        """Second call within cache window returns same result."""
        from django.core.cache import cache
        # Clear cache first
        user_id = request_with_session.session.get('local_user_id', '')
        cache.delete(f'assistant_prompt_{user_id}')

        p1 = build_system_prompt(request_with_session, 'general')
        p2 = build_system_prompt(request_with_session, 'general')
        assert p1 == p2
