"""Tests for analytics tools."""
import pytest

from assistant.tools import discover_tools, get_tool, TOOL_REGISTRY
from assistant.tools.analytics_tools import (
    GetBusinessDashboard, SearchAcrossModules, GetCustomerInsights,
)


@pytest.fixture(autouse=True, scope='module')
def _load_tools():
    """Ensure tools are loaded once for all tests in this module."""
    if 'get_business_dashboard' not in TOOL_REGISTRY:
        discover_tools()


@pytest.mark.django_db
class TestGetBusinessDashboard:
    """Tests for the get_business_dashboard analytics tool."""

    def test_tool_exists(self):
        tool = get_tool('get_business_dashboard')
        assert tool is not None
        assert tool.name == 'get_business_dashboard'

    def test_schema(self):
        tool = get_tool('get_business_dashboard')
        schema = tool.to_openai_schema()
        assert schema['type'] == 'function'
        assert 'period' in schema['parameters']['properties']

    def test_execute_returns_period(self, request_with_session):
        tool = get_tool('get_business_dashboard')
        result = tool.execute({'period': 'today'}, request_with_session)
        assert result['period'] == 'today'
        assert 'start_date' in result

    def test_execute_this_month(self, request_with_session):
        tool = get_tool('get_business_dashboard')
        result = tool.execute({'period': 'this_month'}, request_with_session)
        assert result['period'] == 'this_month'

    def test_execute_this_week(self, request_with_session):
        tool = get_tool('get_business_dashboard')
        result = tool.execute({'period': 'this_week'}, request_with_session)
        assert result['period'] == 'this_week'

    def test_execute_default_period(self, request_with_session):
        tool = get_tool('get_business_dashboard')
        result = tool.execute({}, request_with_session)
        assert result['period'] == 'today'

    def test_has_examples(self):
        tool = get_tool('get_business_dashboard')
        assert len(tool.examples) > 0


@pytest.mark.django_db
class TestSearchAcrossModules:
    """Tests for the search_across_modules analytics tool."""

    def test_tool_exists(self):
        tool = get_tool('search_across_modules')
        assert tool is not None

    def test_execute_returns_structure(self, request_with_session):
        tool = get_tool('search_across_modules')
        result = tool.execute({'query': 'test'}, request_with_session)
        assert 'query' in result
        assert 'total_results' in result
        assert 'results' in result
        assert result['query'] == 'test'

    def test_execute_with_limit(self, request_with_session):
        tool = get_tool('search_across_modules')
        result = tool.execute({'query': 'test', 'limit_per_module': 2}, request_with_session)
        assert result['query'] == 'test'

    def test_has_examples(self):
        tool = get_tool('search_across_modules')
        assert len(tool.examples) > 0


@pytest.mark.django_db
class TestGetCustomerInsights:
    """Tests for the get_customer_insights analytics tool."""

    def test_tool_exists(self):
        tool = get_tool('get_customer_insights')
        assert tool is not None

    def test_execute_no_params(self, request_with_session):
        tool = get_tool('get_customer_insights')
        result = tool.execute({}, request_with_session)
        assert 'error' in result
        assert 'customer_id or customer_name' in result['error']

    def test_execute_customer_not_found_by_id(self, request_with_session):
        tool = get_tool('get_customer_insights')
        result = tool.execute(
            {'customer_id': '00000000-0000-0000-0000-000000000000'},
            request_with_session,
        )
        assert 'error' in result

    def test_execute_customer_not_found_by_name(self, request_with_session):
        tool = get_tool('get_customer_insights')
        result = tool.execute(
            {'customer_name': 'Nonexistent Person XYZZY'},
            request_with_session,
        )
        assert 'error' in result
