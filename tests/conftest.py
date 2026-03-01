"""Pytest fixtures for assistant module tests."""
import uuid
import pytest
from unittest.mock import MagicMock, patch
from django.test import RequestFactory
from django.contrib.sessions.backends.db import SessionStore

from apps.accounts.models import LocalUser
from apps.configuration.models import HubConfig, StoreConfig


@pytest.fixture
def hub_id():
    return uuid.uuid4()


@pytest.fixture
def hub_config(db):
    """Configured HubConfig."""
    HubConfig._clear_cache()
    config = HubConfig.get_solo()
    config.currency = 'EUR'
    config.language = 'es'
    config.os_language = 'es'
    config.timezone = 'Europe/Madrid'
    config.country_code = 'ES'
    config.is_configured = True
    config.hub_jwt = 'test.jwt.token'
    config.save()
    return config


@pytest.fixture
def store_config(db):
    """Configured StoreConfig."""
    from decimal import Decimal
    StoreConfig._clear_cache()
    config = StoreConfig.get_solo()
    config.business_name = 'Test Peluquería'
    config.business_address = 'Calle Test 123, Madrid'
    config.vat_number = 'ES12345678A'
    config.tax_rate = Decimal('21.00')
    config.tax_included = True
    config.is_configured = True
    config.save()
    return config


@pytest.fixture
def admin_user(db, hub_id):
    """Admin user with all permissions."""
    user = LocalUser.objects.create(
        hub_id=hub_id,
        name='Admin Test',
        email='admin@test.com',
        role='admin',
        is_active=True,
    )
    user.set_pin('1234')
    user.save()
    return user


@pytest.fixture
def employee_user(db, hub_id):
    """Employee with limited permissions."""
    user = LocalUser.objects.create(
        hub_id=hub_id,
        name='Employee Test',
        email='employee@test.com',
        role='employee',
        is_active=True,
    )
    user.set_pin('5678')
    user.save()
    return user


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def authenticated_session(admin_user, hub_id):
    """Session dict simulating a logged-in admin."""
    return {
        'local_user_id': str(admin_user.id),
        'user_role': admin_user.role,
        'user_name': admin_user.name,
        'hub_id': str(hub_id),
    }


@pytest.fixture
def request_with_session(rf, authenticated_session):
    """Django request with authenticated session."""
    request = rf.get('/')
    request.session = authenticated_session
    return request


@pytest.fixture
def conversation(db, admin_user):
    """An existing conversation."""
    from assistant.models import AssistantConversation
    return AssistantConversation.objects.create(
        user=admin_user,
        context='general',
    )


@pytest.fixture
def action_log(db, admin_user, conversation):
    """A pending action log."""
    from assistant.models import AssistantActionLog
    return AssistantActionLog.objects.create(
        user=admin_user,
        conversation=conversation,
        tool_name='create_product',
        tool_args={'name': 'Test Product', 'price': '10.00'},
        result={},
        success=False,
        confirmed=False,
    )
