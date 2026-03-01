"""Tests for the assistant tool registry and base class."""
import pytest
from unittest.mock import MagicMock
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from assistant.tools import (
    AssistantTool, register_tool, TOOL_REGISTRY,
    discover_tools, get_tools_for_context, get_tool,
)


class TestAssistantToolBase:
    """Tests for AssistantTool base class."""

    def test_to_openai_schema(self):
        tool = AssistantTool()
        tool.name = 'test_tool'
        tool.description = 'A test tool'
        tool.parameters = {
            'type': 'object',
            'properties': {'name': {'type': 'string'}},
        }

        schema = tool.to_openai_schema()
        assert schema['type'] == 'function'
        assert schema['name'] == 'test_tool'
        assert schema['description'] == 'A test tool'
        assert schema['parameters'] == tool.parameters

    def test_to_openai_schema_with_examples(self):
        tool = AssistantTool()
        tool.name = 'test_tool'
        tool.description = 'A test tool'
        tool.parameters = {'type': 'object', 'properties': {}}
        tool.examples = [{'name': 'Test', 'price': '10.00'}]

        schema = tool.to_openai_schema()
        assert 'Examples:' in schema['description']
        assert '"name": "Test"' in schema['description']

    def test_to_openai_schema_no_examples(self):
        tool = AssistantTool()
        tool.name = 'test_tool'
        tool.description = 'A test tool'
        tool.parameters = {'type': 'object', 'properties': {}}
        tool.examples = []

        schema = tool.to_openai_schema()
        assert 'Examples:' not in schema['description']

    def test_execute_not_implemented(self):
        tool = AssistantTool()
        tool.name = 'test_tool'
        with pytest.raises(NotImplementedError):
            tool.execute({}, None)

    def test_safe_execute_does_not_exist(self):
        """safe_execute catches DoesNotExist and returns friendly error."""

        class Product:
            class DoesNotExist(ObjectDoesNotExist):
                pass

        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise Product.DoesNotExist("Product matching query does not exist.")

        tool = MyTool()
        result = tool.safe_execute({'product_id': 'abc-123'}, None)
        assert 'error' in result
        assert 'not found' in result['error']
        assert 'abc-123' in result['error']

    def test_safe_execute_does_not_exist_no_id(self):
        """safe_execute handles DoesNotExist without id arg."""

        class MyModel:
            class DoesNotExist(ObjectDoesNotExist):
                pass

        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise MyModel.DoesNotExist()

        tool = MyTool()
        result = tool.safe_execute({'name': 'test'}, None)
        assert 'error' in result
        assert 'not found' in result['error']

    def test_safe_execute_validation_error(self):
        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise ValidationError(['Invalid email format', 'Name is required'])

        tool = MyTool()
        result = tool.safe_execute({}, None)
        assert 'error' in result
        assert 'Validation error' in result['error']
        assert 'Invalid email format' in result['error']

    def test_safe_execute_value_error(self):
        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise ValueError("invalid literal for int()")

        tool = MyTool()
        result = tool.safe_execute({}, None)
        assert 'error' in result
        assert 'Invalid input' in result['error']

    def test_safe_execute_type_error(self):
        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise TypeError("expected str, got int")

        tool = MyTool()
        result = tool.safe_execute({}, None)
        assert 'error' in result
        assert 'Invalid input' in result['error']

    def test_safe_execute_unknown_exception_propagates(self):
        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                raise RuntimeError("something unexpected")

        tool = MyTool()
        with pytest.raises(RuntimeError, match="something unexpected"):
            tool.safe_execute({}, None)

    def test_safe_execute_success(self):
        class MyTool(AssistantTool):
            name = 'test'
            def execute(self, args, request):
                return {'success': True, 'data': 'hello'}

        tool = MyTool()
        result = tool.safe_execute({'id': '1'}, None)
        assert result == {'success': True, 'data': 'hello'}


class TestToolRegistry:
    """Tests for tool registration and discovery."""

    def test_register_tool_adds_to_registry(self):
        """@register_tool creates instance and adds to registry."""
        @register_tool
        class TestToolReg(AssistantTool):
            name = 'test_register_unique_789'
            description = 'Test'
            parameters = {'type': 'object', 'properties': {}}
            def execute(self, args, request):
                return {}

        assert 'test_register_unique_789' in TOOL_REGISTRY
        assert isinstance(TOOL_REGISTRY['test_register_unique_789'], TestToolReg)
        # Clean up
        del TOOL_REGISTRY['test_register_unique_789']

    def test_register_tool_without_name_raises(self):
        with pytest.raises(ValueError, match="must define a 'name'"):
            @register_tool
            class BadTool(AssistantTool):
                pass

    def test_get_tool_not_found(self):
        result = get_tool('nonexistent_tool_xyz_999')
        assert result is None


@pytest.mark.django_db
class TestToolDiscovery:
    """Tests for discover_tools() and get_tools_for_context()."""

    def test_discover_tools_loads_core_tools(self):
        discover_tools()
        assert 'get_hub_config' in TOOL_REGISTRY
        assert 'get_store_config' in TOOL_REGISTRY
        assert 'list_modules' in TOOL_REGISTRY
        assert 'list_roles' in TOOL_REGISTRY

    def test_discover_tools_loads_analytics(self):
        discover_tools()
        assert 'get_business_dashboard' in TOOL_REGISTRY
        assert 'search_across_modules' in TOOL_REGISTRY
        assert 'get_customer_insights' in TOOL_REGISTRY

    def test_discover_tools_loads_configure(self):
        discover_tools()
        assert 'execute_plan' in TOOL_REGISTRY

    def test_get_tools_for_context_returns_schemas(self):
        discover_tools()
        tools = get_tools_for_context('general')
        assert len(tools) > 0
        for t in tools:
            assert t['type'] == 'function'
            assert 'name' in t
            assert 'parameters' in t
            assert 'description' in t

    def test_get_tools_for_context_filters_setup_only(self):
        discover_tools()
        general_tools = get_tools_for_context('general')
        setup_tools = get_tools_for_context('setup')

        general_names = {t['name'] for t in general_tools}
        setup_names = {t['name'] for t in setup_tools}

        for name, tool in TOOL_REGISTRY.items():
            if tool.setup_only:
                assert name not in general_names, f"Setup-only tool {name} in general"
                assert name in setup_names, f"Setup-only tool {name} missing from setup"

    def test_get_tools_for_context_permission_filter(self):
        discover_tools()
        mock_user = MagicMock()
        mock_user.has_perm = MagicMock(return_value=False)

        tools = get_tools_for_context('general', user=mock_user)
        tool_names = {t['name'] for t in tools}

        for name, tool in TOOL_REGISTRY.items():
            if tool.required_permission and not tool.setup_only:
                assert name not in tool_names, f"Tool {name} should be filtered"

    def test_get_tools_for_context_no_user_no_filter(self):
        discover_tools()
        tools = get_tools_for_context('general', user=None)
        tool_names = {t['name'] for t in tools}
        assert 'get_hub_config' in tool_names
