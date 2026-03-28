"""Tests for ExecutePlan (configure_tools) and SetupBusiness (setup_tools)."""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

from django.core.cache import cache
from django.test import RequestFactory

from assistant.tools.configure_tools import ExecutePlan, _set_plan_progress
from assistant.tools.setup_tools import SetupBusiness
from apps.configuration.models import HubConfig, StoreConfig


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear Django cache before each test."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def execute_plan():
    return ExecutePlan()


@pytest.fixture
def setup_business():
    return SetupBusiness()


@pytest.fixture
def fake_request(rf, authenticated_session):
    """Request with session, suitable for tool.execute()."""
    request = rf.get('/')
    request.session = authenticated_session
    return request


@pytest.fixture
def unconfigured_hub(db):
    """HubConfig + StoreConfig both unconfigured."""
    HubConfig._clear_cache()
    StoreConfig._clear_cache()
    hub = HubConfig.get_solo()
    hub.is_configured = False
    hub.hub_jwt = 'test.jwt.token'
    hub.save()
    store = StoreConfig.get_solo()
    store.is_configured = False
    store.save()
    return hub, store


# ═══════════════════════════════════════════════════════════════════════
# ExecutePlan tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
class TestExecutePlanEmptySteps:
    """1. Empty steps returns error."""

    def test_empty_list(self, execute_plan, fake_request):
        result = execute_plan.execute({"steps": [], "stop_on_failure": True}, fake_request)
        assert result["success"] is False
        assert "No steps" in result["error"]

    def test_missing_steps_key(self, execute_plan, fake_request):
        result = execute_plan.execute({"stop_on_failure": True}, fake_request)
        assert result["success"] is False
        assert "No steps" in result["error"]


@pytest.mark.django_db
class TestExecutePlanSingleStepSuccess:
    """2. Single step success (set_regional_config)."""

    def test_set_regional_config(self, execute_plan, fake_request, unconfigured_hub):
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {
                        "language": "es",
                        "timezone": "Europe/Madrid",
                        "country_code": "ES",
                        "currency": "EUR",
                    },
                    "description": "Set regional config",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        result = execute_plan.execute(args, fake_request)
        assert result["success"] is True
        assert result["succeeded"] == 1
        assert result["failed"] == 0

        # Verify DB state
        hub = HubConfig.get_solo()
        assert hub.language == "es"
        assert hub.timezone == "Europe/Madrid"
        assert hub.country_code == "ES"
        assert hub.currency == "EUR"


@pytest.mark.django_db
class TestExecutePlanMultipleSteps:
    """3. Multiple steps all succeed — returns success with counts."""

    def test_three_steps_succeed(self, execute_plan, fake_request, unconfigured_hub):
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "en", "timezone": "America/New_York", "country_code": "US", "currency": "USD"},
                    "description": "Regional config",
                    "rollback_action": None,
                    "rollback_params": None,
                },
                {
                    "action": "set_business_info",
                    "params": {"business_name": "Test Shop", "business_address": "123 Main St"},
                    "description": "Business info",
                    "rollback_action": None,
                    "rollback_params": None,
                },
                {
                    "action": "set_tax_config",
                    "params": {"tax_rate": 8.875, "tax_included": False},
                    "description": "Tax config",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        result = execute_plan.execute(args, fake_request)
        assert result["success"] is True
        assert result["total_steps"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert len(result["results"]) == 3
        assert all(r["success"] for r in result["results"])

        store = StoreConfig.get_solo()
        assert store.business_name == "Test Shop"
        assert store.tax_rate == Decimal("8.875")


@pytest.mark.django_db
class TestExecutePlanStepFailureRollback:
    """4. Step failure triggers rollback of completed steps."""

    def test_rollback_on_failure(self, execute_plan, fake_request, unconfigured_hub):
        """When step 2 fails, step 1's rollback_action is invoked."""
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "fr", "timezone": "Europe/Paris", "country_code": "FR", "currency": "EUR"},
                    "description": "Regional config",
                    "rollback_action": "set_regional_config",
                    "rollback_params": {"language": "en", "timezone": "UTC", "country_code": "", "currency": ""},
                },
                {
                    "action": "enable_module",
                    "params": {"module_id": "nonexistent_module_xyz_never_exists"},
                    "description": "Enable fake module",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        # enable_module doesn't raise for missing modules (returns message),
        # so we patch it to raise
        with patch.object(execute_plan, '_enable_module', side_effect=RuntimeError("Module install failed")):
            result = execute_plan.execute(args, fake_request)

        assert result["success"] is False
        assert result["failed"] == 1
        assert result["succeeded"] == 1
        assert len(result["rolled_back"]) == 1
        assert result["rolled_back"][0]["rolled_back"] is True

        # Verify rollback restored the language
        hub = HubConfig.get_solo()
        assert hub.language == "en"


@pytest.mark.django_db
class TestExecutePlanNoRollbackAction:
    """5. Step failure with no rollback_action defined."""

    def test_no_rollback_action_skips_gracefully(self, execute_plan, fake_request, unconfigured_hub):
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "de", "country_code": "DE", "currency": "EUR", "timezone": "Europe/Berlin"},
                    "description": "Regional config",
                    "rollback_action": None,
                    "rollback_params": None,
                },
                {
                    "action": "set_business_info",
                    "params": {"business_name": "Deutsche Laden"},
                    "description": "Business info",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        # Make step 2 fail
        with patch.object(execute_plan, '_set_business_info', side_effect=ValueError("forced error")):
            result = execute_plan.execute(args, fake_request)

        assert result["success"] is False
        assert len(result["rolled_back"]) == 1
        # The rollback entry for step 1 says it was NOT rolled back because no rollback_action
        assert result["rolled_back"][0]["rolled_back"] is False
        assert "No rollback_action defined" in result["rolled_back"][0]["reason"]


@pytest.mark.django_db
class TestExecutePlanStopOnFailureFalse:
    """6. stop_on_failure=False continues after failure."""

    def test_continues_after_failure(self, execute_plan, fake_request, unconfigured_hub):
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"},
                    "description": "Step 1",
                    "rollback_action": None,
                    "rollback_params": None,
                },
                {
                    "action": "set_business_info",
                    "params": {"business_name": "Tienda"},
                    "description": "Step 2 (will fail)",
                    "rollback_action": None,
                    "rollback_params": None,
                },
                {
                    "action": "set_tax_config",
                    "params": {"tax_rate": 21.0, "tax_included": True},
                    "description": "Step 3",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": False,
        }
        with patch.object(execute_plan, '_set_business_info', side_effect=ValueError("forced")):
            result = execute_plan.execute(args, fake_request)

        assert result["success"] is False
        assert result["total_steps"] == 3
        assert result["succeeded"] == 2  # steps 1 and 3
        assert result["failed"] == 1  # step 2
        # No rollback when stop_on_failure is False
        assert result["rolled_back"] == []

        # Step 3 still executed
        store = StoreConfig.get_solo()
        assert store.tax_rate == Decimal("21")
        assert store.tax_included is True


class TestSetPlanProgress:
    """7. _set_plan_progress writes to cache."""

    def test_writes_to_cache(self):
        _set_plan_progress("req-123", 1, 3, "set_regional_config", "running", "Step 1/3: config...")
        cached = cache.get("assistant_progress_req-123")
        assert cached is not None
        assert cached["type"] == "tool"
        assert "Step 1/3: config..." in cached["data"]

    def test_default_message_format(self):
        _set_plan_progress("req-456", 2, 5, "create_role", "done")
        cached = cache.get("assistant_progress_req-456")
        assert cached is not None
        assert "Step 2/5" in cached["data"]
        assert "create_role" in cached["data"]
        assert "done" in cached["data"]

    def test_no_request_id_does_nothing(self):
        _set_plan_progress(None, 1, 1, "test", "running")
        # Should not crash and no cache entry
        assert cache.get("assistant_progress_None") is None

    def test_empty_request_id_does_nothing(self):
        _set_plan_progress("", 1, 1, "test", "running")
        assert cache.get("assistant_progress_") is None


@pytest.mark.django_db
class TestExecutePlanAutoCompleteSetup:
    """8. Auto-complete setup after successful plan."""

    def test_auto_completes_setup(self, execute_plan, fake_request, unconfigured_hub):
        hub, store = unconfigured_hub
        assert hub.is_configured is False
        assert store.is_configured is False

        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"},
                    "description": "Regional",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        result = execute_plan.execute(args, fake_request)
        assert result["success"] is True

        # Both configs should now be marked configured
        hub.refresh_from_db()
        store.refresh_from_db()
        assert hub.is_configured is True
        assert store.is_configured is True

    def test_no_auto_complete_on_failure(self, execute_plan, fake_request, unconfigured_hub):
        hub, store = unconfigured_hub
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"},
                    "description": "Regional (will fail)",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        with patch.object(execute_plan, '_set_regional_config', side_effect=ValueError("boom")):
            result = execute_plan.execute(args, fake_request)

        assert result["success"] is False
        hub.refresh_from_db()
        store.refresh_from_db()
        assert hub.is_configured is False
        assert store.is_configured is False


@pytest.mark.django_db
class TestExecutePlanInstallBlueprint:
    """9. _install_blueprint passes defer_restart=True."""

    @patch('assistant.tools.configure_tools.BlueprintService', create=True)
    def test_install_blueprint_defer_restart(self, mock_bp_cls, execute_plan, fake_request, unconfigured_hub):
        mock_install = MagicMock(return_value={
            'modules_installed': 3,
            'roles_created': 2,
            'installed_module_ids': ['inventory', 'sales', 'pos'],
        })
        # BlueprintService is imported inside _install_blueprint, so we patch the import path
        with patch(
            'apps.core.services.blueprint_service.BlueprintService.install_blueprint',
            mock_install,
        ):
            with patch('assistant.tools.discover_tools'):
                result = execute_plan._install_blueprint(
                    {"type_codes": ["restaurant"]}, fake_request,
                )

        assert result["modules_installed"] == 3
        assert result["roles_created"] == 2
        mock_install.assert_called_once()
        call_kwargs = mock_install.call_args
        assert call_kwargs.kwargs.get('defer_restart') is True or call_kwargs[1].get('defer_restart') is True

    @patch('apps.core.services.blueprint_service.BlueprintService.install_blueprint')
    def test_install_blueprint_saves_session(self, mock_install, execute_plan, fake_request, unconfigured_hub):
        mock_install.return_value = {
            'modules_installed': 2,
            'roles_created': 1,
            'installed_module_ids': ['inventory', 'customers'],
        }
        with patch('assistant.tools.discover_tools'):
            execute_plan._install_blueprint({"type_codes": ["retail"]}, fake_request)

        assert 'assistant_post_install' in fake_request.session
        assert fake_request.session['assistant_post_install']['type_codes'] == ['retail']

    def test_install_blueprint_no_type_codes_raises(self, execute_plan, fake_request, unconfigured_hub):
        # Ensure hub has no selected_business_types
        hub = HubConfig.get_solo()
        hub.selected_business_types = []
        hub.save()

        with pytest.raises(ValueError, match="type_codes is required"):
            execute_plan._install_blueprint({}, fake_request)


@pytest.mark.django_db
class TestExecutePlanUnknownAction:
    """10. Unknown action delegates to TOOL_REGISTRY."""

    def test_delegates_to_registered_tool(self, execute_plan, fake_request, unconfigured_hub):
        mock_tool = MagicMock()
        mock_tool.safe_execute.return_value = {"created": True, "id": "42"}

        with patch('assistant.tools.get_tool', return_value=mock_tool) as mock_get:
            result = execute_plan._execute_step("create_product", {"name": "Widget"}, fake_request)

        mock_get.assert_called_with("create_product")
        mock_tool.safe_execute.assert_called_once_with({"name": "Widget"}, fake_request)
        assert result["created"] is True

    def test_unknown_action_raises_value_error(self, execute_plan, fake_request, unconfigured_hub):
        with patch('assistant.tools.get_tool', return_value=None):
            with patch('assistant.tools.discover_tools'):
                with pytest.raises(ValueError, match="Unknown action"):
                    execute_plan._execute_step("totally_fake_action", {}, fake_request)

    def test_rediscovers_tools_on_miss(self, execute_plan, fake_request, unconfigured_hub):
        """If get_tool returns None the first time, discover_tools is called and retried."""
        mock_tool = MagicMock()
        mock_tool.safe_execute.return_value = {"ok": True}

        # First call returns None, second call (after discover) returns the tool
        with patch('assistant.tools.get_tool', side_effect=[None, mock_tool]):
            with patch('assistant.tools.discover_tools') as mock_discover:
                result = execute_plan._execute_step("late_registered_tool", {}, fake_request)

        mock_discover.assert_called_once()
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════════════
# SetupBusiness tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
class TestSetupBusinessRegionalOnly:
    """1. Basic setup with regional config only (empty business_type_codes)."""

    def test_regional_config_applied(self, setup_business, fake_request):
        HubConfig._clear_cache()
        StoreConfig._clear_cache()
        HubConfig.get_solo()
        StoreConfig.get_solo()

        args = {
            "language": "es",
            "timezone": "Europe/Madrid",
            "country_code": "ES",
            "currency": "EUR",
            "business_name": "Mi Tienda",
            "business_address": "Calle Mayor 1",
            "vat_number": "",
            "tax_rate": 21.0,
            "tax_included": True,
            "business_type_codes": [],
        }
        result = setup_business.execute(args, fake_request)
        assert result["success"] is True
        assert result["regional"]["language"] == "es"
        assert result["regional"]["currency"] == "EUR"
        assert result["business"]["name"] == "Mi Tienda"
        assert "blueprint" not in result  # No blueprint when empty codes

        hub = HubConfig.get_solo()
        assert hub.is_configured is True


@pytest.mark.django_db
class TestSetupBusinessFull:
    """2. Full setup with business info + tax."""

    def test_full_setup_fields(self, setup_business, fake_request):
        HubConfig._clear_cache()
        StoreConfig._clear_cache()
        HubConfig.get_solo()
        StoreConfig.get_solo()

        args = {
            "language": "de",
            "timezone": "Europe/Berlin",
            "country_code": "DE",
            "currency": "EUR",
            "business_name": "Bäckerei Schmidt",
            "business_address": "Berliner Str. 42, Berlin",
            "vat_number": "DE123456789",
            "tax_rate": 19.0,
            "tax_included": True,
            "business_type_codes": [],
        }
        result = setup_business.execute(args, fake_request)
        assert result["success"] is True
        assert result["business"]["tax_rate"] == "19.0"
        assert result["business"]["tax_included"] is True

        store = StoreConfig.get_solo()
        assert store.business_name == "Bäckerei Schmidt"
        assert store.vat_number == "DE123456789"
        assert store.is_configured is True


@pytest.mark.django_db
class TestSetupBusinessBlueprint:
    """3. Setup with blueprint installation (mock BlueprintService)."""

    @patch('apps.core.services.blueprint_service.BlueprintService.install_blueprint')
    def test_blueprint_installed(self, mock_install, setup_business, fake_request):
        HubConfig._clear_cache()
        StoreConfig._clear_cache()
        HubConfig.get_solo()
        StoreConfig.get_solo()

        mock_install.return_value = {
            'modules_installed': ['inventory', 'sales', 'pos'],
            'roles_created': ['admin', 'manager', 'waiter'],
        }

        args = {
            "language": "es",
            "timezone": "Europe/Madrid",
            "country_code": "ES",
            "currency": "EUR",
            "business_name": "Restaurante El Buen Comer",
            "business_address": "Paseo del Prado 10",
            "vat_number": "ESB12345678",
            "tax_rate": 10.0,
            "tax_included": True,
            "business_type_codes": ["restaurant"],
        }
        result = setup_business.execute(args, fake_request)
        assert result["success"] is True
        assert result["blueprint"]["installed"] is True
        assert result["blueprint"]["modules"] == ['inventory', 'sales', 'pos']
        assert result["blueprint"]["roles"] == ['admin', 'manager', 'waiter']

        mock_install.assert_called_once()


@pytest.mark.django_db
class TestSetupBusinessBlueprintFailure:
    """4. Blueprint install failure doesn't crash."""

    @patch('apps.core.services.blueprint_service.BlueprintService.install_blueprint')
    def test_blueprint_failure_graceful(self, mock_install, setup_business, fake_request):
        HubConfig._clear_cache()
        StoreConfig._clear_cache()
        HubConfig.get_solo()
        StoreConfig.get_solo()

        mock_install.side_effect = ConnectionError("Cloud API unreachable")

        args = {
            "language": "es",
            "timezone": "Europe/Madrid",
            "country_code": "ES",
            "currency": "EUR",
            "business_name": "Tienda Fallida",
            "business_address": "Calle Rota 1",
            "vat_number": "",
            "tax_rate": 21.0,
            "tax_included": True,
            "business_type_codes": ["retail"],
        }
        result = setup_business.execute(args, fake_request)
        # Setup itself succeeds even though blueprint failed
        assert result["success"] is True
        assert result["blueprint"]["installed"] is False
        assert "Cloud API unreachable" in result["blueprint"]["error"]

        # Hub is still marked configured
        hub = HubConfig.get_solo()
        assert hub.is_configured is True


@pytest.mark.django_db
class TestSetupBusinessDeferRestart:
    """5. defer_restart=True is passed to BlueprintService."""

    @patch('apps.core.services.blueprint_service.BlueprintService.install_blueprint')
    def test_defer_restart_passed(self, mock_install, setup_business, fake_request):
        HubConfig._clear_cache()
        StoreConfig._clear_cache()
        HubConfig.get_solo()
        StoreConfig.get_solo()

        mock_install.return_value = {
            'modules_installed': ['inventory'],
            'roles_created': [],
        }

        args = {
            "language": "es",
            "timezone": "Europe/Madrid",
            "country_code": "ES",
            "currency": "EUR",
            "business_name": "Test",
            "business_address": "Addr",
            "vat_number": "",
            "tax_rate": 21.0,
            "tax_included": True,
            "business_type_codes": ["retail"],
        }
        setup_business.execute(args, fake_request)

        mock_install.assert_called_once()
        _, kwargs = mock_install.call_args
        assert kwargs.get('defer_restart') is True


# ═══════════════════════════════════════════════════════════════════════
# ExecutePlan helper tests
# ═══════════════════════════════════════════════════════════════════════


class TestResolveRollbackParams:
    """Test _resolve_rollback_params placeholder substitution."""

    def test_substitutes_result_field(self):
        plan = ExecutePlan()
        resolved = plan._resolve_rollback_params(
            {"id": "{result.category_id}"}, {"category_id": "cat-42"},
        )
        assert resolved == {"id": "cat-42"}

    def test_no_placeholder_passes_through(self):
        plan = ExecutePlan()
        resolved = plan._resolve_rollback_params(
            {"name": "literal"}, {"id": "123"},
        )
        assert resolved == {"name": "literal"}

    def test_empty_params(self):
        plan = ExecutePlan()
        assert plan._resolve_rollback_params({}, {"id": "1"}) == {}
        assert plan._resolve_rollback_params(None, {"id": "1"}) == {}

    def test_non_dict_result(self):
        plan = ExecutePlan()
        resolved = plan._resolve_rollback_params({"id": "{result.id}"}, "not a dict")
        assert resolved == {"id": "{result.id}"}


class TestFriendlyError:
    """Test _friendly_error message conversion."""

    def test_duplicate_key(self):
        plan = ExecutePlan()
        msg = plan._friendly_error(Exception("duplicate key value violates unique constraint"))
        assert "Already exists" in msg

    def test_relation_not_found(self):
        plan = ExecutePlan()
        msg = plan._friendly_error(Exception('relation "inventory_product" does not exist'))
        assert "module may not be installed" in msg

    def test_not_null(self):
        plan = ExecutePlan()
        msg = plan._friendly_error(Exception("null value in column 'name' violates NOT NULL constraint"))
        assert "required field" in msg

    def test_generic_error(self):
        plan = ExecutePlan()
        msg = plan._friendly_error(Exception("something weird"))
        assert msg == "something weird"


class TestBuildSummary:
    """Test _build_summary output."""

    def test_all_success(self):
        plan = ExecutePlan()
        results = [{"success": True}, {"success": True}]
        summary = plan._build_summary(results, [], [])
        assert "All 2 steps completed successfully" in summary

    def test_with_errors_and_rollback(self):
        plan = ExecutePlan()
        results = [{"success": True}, {"success": False}]
        rolled_back = [{"rolled_back": True}]
        errors = ["Step 2 (create_product): Already exists"]
        summary = plan._build_summary(results, rolled_back, errors)
        assert "1/2 steps succeeded" in summary
        assert "Rolled back 1" in summary


class TestAliasResolution:
    """Test that LLM action aliases are resolved in _execute_step."""

    def test_create_staff_member_alias(self, execute_plan):
        mock_tool = MagicMock()
        mock_tool.safe_execute.return_value = {"created": True}

        with patch('assistant.tools.get_tool', return_value=mock_tool) as mock_get:
            execute_plan._execute_step("create_staff_member", {"name": "Ana"}, MagicMock())

        # Should resolve alias to create_employee
        mock_get.assert_called_with("create_employee")

    def test_add_product_alias(self, execute_plan):
        mock_tool = MagicMock()
        mock_tool.safe_execute.return_value = {"created": True}

        with patch('assistant.tools.get_tool', return_value=mock_tool) as mock_get:
            execute_plan._execute_step("add_product", {"name": "Widget"}, MagicMock())

        mock_get.assert_called_with("create_product")


@pytest.mark.django_db
class TestExecutePlanProgressTracking:
    """Test that plan execution writes progress to cache."""

    def test_progress_written_during_execution(self, execute_plan, fake_request, unconfigured_hub):
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"},
                    "description": "Regional setup",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
            "_plan_request_id": "test-req-progress",
        }
        execute_plan.execute(args, fake_request)

        # After execution, the last progress entry should be "done"
        cached = cache.get("assistant_progress_test-req-progress")
        assert cached is not None
        assert "done" in cached["data"]


@pytest.mark.django_db
class TestExecutePlanNestedParams:
    """Test unwrapping nested params from LLM."""

    def test_unwraps_parameters_key(self, execute_plan, fake_request, unconfigured_hub):
        """LLM sometimes wraps params as {"parameters": {...actual...}}."""
        args = {
            "steps": [
                {
                    "action": "set_regional_config",
                    "params": {
                        "parameters": {
                            "language": "it",
                            "timezone": "Europe/Rome",
                            "country_code": "IT",
                            "currency": "EUR",
                        },
                    },
                    "description": "Italian config",
                    "rollback_action": None,
                    "rollback_params": None,
                },
            ],
            "stop_on_failure": True,
        }
        result = execute_plan.execute(args, fake_request)
        assert result["success"] is True

        hub = HubConfig.get_solo()
        assert hub.language == "it"
        assert hub.country_code == "IT"
