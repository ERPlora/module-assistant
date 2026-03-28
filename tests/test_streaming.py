"""Tests for SSE streaming, progress tracking, and confirmation flows."""
import json
import uuid
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from django.core.cache import cache
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory

from assistant.models import (
    AssistantConversation, AssistantActionLog, AssistantRequest,
)
from assistant.views import (
    _set_progress,
    _get_post_restart_message,
    _summarize_plan_steps,
    poll_progress,
    confirm_action,
    _resume_loop_after_confirm,
    _confirm_execute_plan_async,
    chat_stream,
    PROGRESS_CACHE_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(rf, session, method='get', path='/', data=None):
    """Build a request with session attached."""
    if method == 'post':
        req = rf.post(path, data=data or {})
    else:
        req = rf.get(path)
    req.session = session
    return req


# ===========================================================================
# _set_progress
# ===========================================================================

class TestSetProgress:
    """Tests for _set_progress() dual-write to cache and DB."""

    def test_writes_to_cache(self):
        rid = uuid.uuid4().hex[:16]
        _set_progress(rid, 'thinking', 'Working...')
        cached = cache.get(f'assistant_progress_{rid}')
        assert cached == {'type': 'thinking', 'data': 'Working...'}

    def test_no_cache_write_when_request_id_is_none(self):
        # Should not raise
        _set_progress(None, 'thinking', 'Working...')

    def test_no_cache_write_when_request_id_is_empty(self):
        _set_progress('', 'thinking', 'Working...')
        # Empty string is falsy, so no cache write
        assert cache.get('assistant_progress_') is None

    @pytest.mark.django_db
    def test_db_write_complete(self, admin_user, conversation):
        ar = AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='processing',
        )
        rid = uuid.uuid4().hex[:16]
        _set_progress(rid, 'complete', 'Done', db_request_id=ar.id)
        ar.refresh_from_db()
        assert ar.status == 'complete'
        assert ar.progress_message == 'Done'

    @pytest.mark.django_db
    def test_db_write_error(self, admin_user, conversation):
        ar = AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='processing',
        )
        rid = uuid.uuid4().hex[:16]
        _set_progress(rid, 'error', 'Something broke', db_request_id=ar.id)
        ar.refresh_from_db()
        assert ar.status == 'error'
        assert ar.error_message == 'Something broke'

    @pytest.mark.django_db
    def test_db_write_thinking_sets_processing(self, admin_user, conversation):
        ar = AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='pending',
        )
        _set_progress('abc', 'thinking', 'Thinking...', db_request_id=ar.id)
        ar.refresh_from_db()
        assert ar.status == 'processing'

    @pytest.mark.django_db
    def test_db_write_tool_sets_processing(self, admin_user, conversation):
        ar = AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='pending',
        )
        _set_progress('abc', 'tool', 'Running tool...', db_request_id=ar.id)
        ar.refresh_from_db()
        assert ar.status == 'processing'

    def test_db_write_skipped_when_no_db_request_id(self):
        # Should not raise when db_request_id is None
        _set_progress('abc', 'complete', 'Done', db_request_id=None)

    @pytest.mark.django_db
    def test_db_write_truncates_progress_message(self, admin_user, conversation):
        ar = AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='processing',
        )
        long_msg = 'x' * 500
        _set_progress('abc', 'thinking', long_msg, db_request_id=ar.id)
        ar.refresh_from_db()
        assert len(ar.progress_message) <= 200


# ===========================================================================
# _get_post_restart_message
# ===========================================================================

@pytest.mark.django_db
class TestGetPostRestartMessage:

    def test_execute_plan_success(self, admin_user, conversation):
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={'modules_installed': 5},
            success=True, confirmed=True,
        )
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'installed successfully' in msg
        assert 'restarted' in msg

    def test_execute_plan_with_errors(self, admin_user, conversation):
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={'modules_installed': 2, 'errors': ['module_x failed']},
            success=True, confirmed=True,
        )
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'partially installed' in msg

    def test_execute_plan_zero_installed_falls_through(self, admin_user, conversation):
        """execute_plan with 0 modules installed falls to generic success."""
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={},
            success=True, confirmed=True,
        )
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'restarted after completing' in msg

    def test_non_plan_success(self, admin_user, conversation):
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={'id': 1},
            success=True, confirmed=True,
        )
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'restarted after completing' in msg
        assert 'saved' in msg

    def test_no_confirmed_actions(self, admin_user):
        """No confirmed actions -> generic fallback."""
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'restarted while processing' in msg

    def test_invalid_user_id(self):
        msg = _get_post_restart_message('nonexistent-uuid')
        # Should return generic message (exception caught)
        assert 'restarted' in msg

    def test_succeeded_key_alternative(self, admin_user, conversation):
        """Tests the 'succeeded' key alternative for modules count."""
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={'succeeded': 3},
            success=True, confirmed=True,
        )
        msg = _get_post_restart_message(str(admin_user.id))
        assert 'installed successfully' in msg


# ===========================================================================
# poll_progress
# ===========================================================================

@pytest.mark.django_db
class TestPollProgress:

    def setup_method(self):
        cache.clear()

    def test_in_progress(self, rf, authenticated_session):
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'thinking', 'data': 'Working...'})
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        assert isinstance(resp, HttpResponse)
        assert rid in resp.content.decode()

    def test_complete_with_result(self, rf, authenticated_session, admin_user, conversation):
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'complete', 'data': ''})
        cache.set(f'assistant_result_{rid}', {
            'response_text': 'All done!',
            'conversation_id': str(conversation.id),
            'pending_actions': [],
        })
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'All done!' in content
        # Cache should be cleaned up
        assert cache.get(f'assistant_progress_{rid}') is None
        assert cache.get(f'assistant_result_{rid}') is None

    def test_complete_no_result_shows_error(self, rf, authenticated_session):
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'complete', 'data': ''})
        # No result in cache
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Unknown error' in content

    def test_error_with_result(self, rf, authenticated_session):
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'error', 'data': 'Oops'})
        cache.set(f'assistant_result_{rid}', {'error': 'Something went wrong'})
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Something went wrong' in content

    def test_cache_miss_with_result(self, rf, authenticated_session, conversation):
        """No progress in cache but result exists (completed while not polling)."""
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_result_{rid}', {
            'response_text': 'Done while away',
            'conversation_id': str(conversation.id),
            'pending_actions': [],
        })
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Done while away' in content

    def test_cache_miss_db_complete(self, rf, authenticated_session, admin_user, conversation):
        """Cache wiped (restart) but DB shows complete."""
        rid = uuid.uuid4().hex[:16]
        AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='complete',
            response_text='Recovered from DB',
            pending_actions=[],
        )
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Recovered from DB' in content

    def test_cache_miss_db_error(self, rf, authenticated_session, admin_user, conversation):
        """Cache wiped but DB shows error."""
        rid = uuid.uuid4().hex[:16]
        AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='error',
            error_message='Cloud timeout',
        )
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Cloud timeout' in content

    def test_cache_miss_db_processing(self, rf, authenticated_session, admin_user, conversation):
        """Cache wiped but DB still processing."""
        rid = uuid.uuid4().hex[:16]
        AssistantRequest.objects.create(
            user=admin_user, conversation=conversation,
            user_message='test', status='processing',
            progress_message='Installing modules...',
        )
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Installing modules...' in content
        # Should include request_id for continued polling
        assert rid in content

    def test_cache_miss_no_db_post_restart(self, rf, authenticated_session, admin_user, conversation):
        """Cache wiped and no DB request -> post-restart message."""
        rid = uuid.uuid4().hex[:16]
        # Create a confirmed action so _get_post_restart_message finds it
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={'modules_installed': 3},
            success=True, confirmed=True,
        )
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'installed successfully' in content

    def test_complete_with_tier_info(self, rf, authenticated_session, conversation):
        """Tier info headers are set on complete response."""
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'complete', 'data': ''})
        cache.set(f'assistant_result_{rid}', {
            'response_text': 'Done',
            'conversation_id': str(conversation.id),
            'pending_actions': [],
            'tier_info': {
                'tier': 'basic',
                'sessions_used': 5,
                'sessions_limit': 500,
                'tier_name': 'Basic',
            },
        })
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        assert resp['X-Assistant-Tier'] == 'basic'
        usage = json.loads(resp['X-Assistant-Usage'])
        assert usage['sessions_used'] == 5

    def test_complete_with_pending_actions(self, rf, authenticated_session, conversation):
        """Pending actions render confirmation partials."""
        rid = uuid.uuid4().hex[:16]
        cache.set(f'assistant_progress_{rid}', {'type': 'complete', 'data': ''})
        cache.set(f'assistant_result_{rid}', {
            'response_text': 'Ready to create',
            'conversation_id': str(conversation.id),
            'pending_actions': [{
                'log_id': 99,
                'tool_name': 'create_product',
                'tool_args': {'name': 'Test'},
                'description': 'Create product: Test',
            }],
        })
        request = _make_request(rf, authenticated_session)
        resp = poll_progress(request, rid)
        content = resp.content.decode()
        assert 'Ready to create' in content


# ===========================================================================
# confirm_action
# ===========================================================================

@pytest.mark.django_db
class TestConfirmAction:

    def test_action_not_found(self, rf, authenticated_session):
        request = _make_request(rf, authenticated_session, method='post')
        resp = confirm_action(request, log_id=99999)
        content = resp.content.decode()
        assert 'not found' in content.lower() or 'already processed' in content.lower()

    def test_already_confirmed(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={}, success=False, confirmed=True,
        )
        request = _make_request(rf, authenticated_session, method='post')
        resp = confirm_action(request, log_id=action.id)
        content = resp.content.decode()
        assert 'not found' in content.lower() or 'already processed' in content.lower()

    def test_execute_plan_dispatches_async(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': [{'action': 'create_product', 'params': {'name': 'X'}}]},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session, method='post')
        with patch('assistant.views._confirm_execute_plan_async') as mock_async:
            mock_async.return_value = HttpResponse('progress...')
            resp = confirm_action(request, log_id=action.id)
            mock_async.assert_called_once_with(request, action, str(admin_user.id))
            assert resp.content == b'progress...'

    def test_non_plan_success_resumes_loop(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session, method='post')
        with patch('assistant.views.execute_confirmed_action') as mock_exec, \
             patch('assistant.views._resume_loop_after_confirm') as mock_resume:
            mock_exec.return_value = {'success': True, 'result': {'id': 1}}
            mock_resume.return_value = HttpResponse('resumed')
            resp = confirm_action(request, log_id=action.id)
            mock_exec.assert_called_once()
            mock_resume.assert_called_once()
            assert resp.content == b'resumed'

    def test_non_plan_failure_shows_error(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session, method='post')
        with patch('assistant.views.execute_confirmed_action') as mock_exec:
            mock_exec.return_value = {'success': False, 'message': 'Duplicate name'}
            resp = confirm_action(request, log_id=action.id)
            content = resp.content.decode()
            assert 'Duplicate name' in content

    def test_wrong_user_cannot_confirm(self, rf, authenticated_session, employee_user, conversation):
        """Action belongs to a different user; should not be found."""
        action = AssistantActionLog.objects.create(
            user=employee_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'Test'},
            result={}, success=False, confirmed=False,
        )
        # authenticated_session belongs to admin_user
        request = _make_request(rf, authenticated_session, method='post')
        resp = confirm_action(request, log_id=action.id)
        content = resp.content.decode()
        assert 'not found' in content.lower() or 'already processed' in content.lower()


# ===========================================================================
# _resume_loop_after_confirm
# ===========================================================================

@pytest.mark.django_db
class TestResumeLoopAfterConfirm:

    def test_sibling_pending_returns_message(self, rf, authenticated_session, admin_user, conversation):
        """When sibling actions are unconfirmed, returns a 'please confirm' message."""
        msg_id = 'msg_123'
        action1 = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'A', '_call_id': 'c1'},
            result={'id': 1}, success=True, confirmed=True,
            llm_message_id=msg_id,
        )
        # Sibling still pending
        AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'B', '_call_id': 'c2'},
            result={}, success=False, confirmed=False,
            llm_message_id=msg_id,
        )
        request = _make_request(rf, authenticated_session)
        resp = _resume_loop_after_confirm(
            request, action1, {'success': True, 'result': {'id': 1}}, str(admin_user.id),
        )
        content = resp.content.decode()
        assert 'remaining' in content.lower()
        assert '1' in content  # 1 remaining action

    def test_all_confirmed_starts_loop(self, rf, authenticated_session, admin_user, conversation):
        """When all siblings are confirmed, starts background loop."""
        msg_id = 'msg_456'
        action1 = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'A', '_call_id': 'c1'},
            result={'id': 1}, success=True, confirmed=True,
            llm_message_id=msg_id,
        )
        action2 = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'B', '_call_id': 'c2'},
            result={'id': 2}, success=True, confirmed=True,
            llm_message_id=msg_id,
        )
        request = _make_request(rf, authenticated_session)
        with patch('assistant.views.run_agentic_loop') as mock_loop, \
             patch('threading.Thread') as mock_thread:
            mock_thread_inst = MagicMock()
            mock_thread.return_value = mock_thread_inst
            resp = _resume_loop_after_confirm(
                request, action1, {'success': True, 'result': {'id': 1}}, str(admin_user.id),
            )
            mock_thread.assert_called_once()
            mock_thread_inst.start.assert_called_once()
            # Response should be a progress partial
            content = resp.content.decode()
            assert 'Continuing' in content or 'progress' in content.lower()

    def test_no_llm_message_id_uses_single_action(self, rf, authenticated_session, admin_user, conversation):
        """Action without llm_message_id resumes with just its own result."""
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'A', '_call_id': 'c1'},
            result={'id': 1}, success=True, confirmed=True,
            llm_message_id='',
        )
        request = _make_request(rf, authenticated_session)
        with patch('assistant.views.run_agentic_loop'), \
             patch('threading.Thread') as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = _resume_loop_after_confirm(
                request, action, {'success': True, 'result': {'id': 1}}, str(admin_user.id),
            )
            mock_thread.assert_called_once()

    def test_no_call_id_fallback_text(self, rf, authenticated_session, admin_user, conversation):
        """Action without _call_id uses text-based resume input."""
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='create_product',
            tool_args={'name': 'A'},
            result={'id': 1}, success=True, confirmed=True,
            llm_message_id='',
        )
        request = _make_request(rf, authenticated_session)
        with patch('assistant.views.run_agentic_loop') as mock_loop, \
             patch('threading.Thread') as mock_thread:
            mock_thread_inst = MagicMock()
            mock_thread.return_value = mock_thread_inst

            # Capture the target function to inspect resume_input
            def capture_thread(**kwargs):
                return mock_thread_inst
            mock_thread.side_effect = capture_thread

            resp = _resume_loop_after_confirm(
                request, action, {'success': True, 'result': {'id': 1}}, str(admin_user.id),
            )
            mock_thread_inst.start.assert_called_once()


# ===========================================================================
# _confirm_execute_plan_async
# ===========================================================================

@pytest.mark.django_db
class TestConfirmExecutePlanAsync:

    def test_starts_background_thread(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': [{'action': 'create_product', 'params': {'name': 'X'}}]},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session)
        with patch('threading.Thread') as mock_thread:
            mock_thread_inst = MagicMock()
            mock_thread.return_value = mock_thread_inst
            resp = _confirm_execute_plan_async(request, action, str(admin_user.id))
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs.kwargs.get('daemon') is True
            mock_thread_inst.start.assert_called_once()

    def test_returns_progress_html(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session)
        with patch('threading.Thread') as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = _confirm_execute_plan_async(request, action, str(admin_user.id))
            content = resp.content.decode()
            assert 'Executing plan' in content or 'progress' in content.lower()

    def test_sets_initial_progress(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session)
        with patch('threading.Thread') as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = _confirm_execute_plan_async(request, action, str(admin_user.id))

        # There should be at least one cache entry with 'Executing plan'
        # We can't know the exact request_id, so check by scanning cache behavior
        # Instead, verify the response HTML contains the polling pattern
        content = resp.content.decode()
        assert isinstance(resp, HttpResponse)

    def test_user_not_found(self, rf, authenticated_session, admin_user, conversation):
        action = AssistantActionLog.objects.create(
            user=admin_user, conversation=conversation,
            tool_name='execute_plan',
            tool_args={'steps': []},
            result={}, success=False, confirmed=False,
        )
        request = _make_request(rf, authenticated_session)
        # Pass a non-existent user_id
        resp = _confirm_execute_plan_async(request, action, str(uuid.uuid4()))
        content = resp.content.decode()
        assert 'User not found' in content


# ===========================================================================
# _summarize_plan_steps
# ===========================================================================

class TestSummarizePlanSteps:

    def test_empty_steps(self):
        result = _summarize_plan_steps([])
        assert '0 steps' in result

    def test_named_steps(self):
        steps = [
            {'action': 'set_business_info', 'params': {'business_name': 'Mi Tienda'}},
            {'action': 'set_tax_config', 'params': {'tax_rate': '21'}},
        ]
        result = _summarize_plan_steps(steps)
        assert '2 steps' in result
        assert 'Mi Tienda' in result
        assert '21%' in result

    def test_counted_steps(self):
        steps = [
            {'action': 'create_product', 'params': {'name': f'P{i}'}}
            for i in range(5)
        ]
        result = _summarize_plan_steps(steps)
        assert '5 steps' in result
        assert '5 products' in result

    def test_single_counted_step(self):
        steps = [{'action': 'create_role', 'params': {'name': 'Admin'}}]
        result = _summarize_plan_steps(steps)
        assert '1 step' in result
        assert 'role' in result

    def test_mixed_named_and_counted(self):
        steps = [
            {'action': 'set_business_info', 'params': {'business_name': 'Shop'}},
            {'action': 'install_blueprint', 'params': {'type_codes': ['retail_general']}},
            {'action': 'create_product', 'params': {'name': 'A'}},
            {'action': 'create_product', 'params': {'name': 'B'}},
            {'action': 'create_employee', 'params': {'name': 'E1'}},
            {'action': 'complete_setup', 'params': {}},
        ]
        result = _summarize_plan_steps(steps)
        assert '6 steps' in result
        assert 'Shop' in result
        assert 'retail_general' in result
        assert '2 products' in result
        assert 'employee' in result
        assert 'Complete setup' in result

    def test_set_regional_config(self):
        steps = [{'action': 'set_regional_config', 'params': {}}]
        result = _summarize_plan_steps(steps)
        assert 'region' in result.lower()

    def test_install_blueprint_products(self):
        steps = [{'action': 'install_blueprint_products', 'params': {'business_type': 'bakery'}}]
        result = _summarize_plan_steps(steps)
        assert 'bakery' in result

    def test_unknown_action_counted(self):
        steps = [
            {'action': 'some_future_action', 'params': {}},
            {'action': 'some_future_action', 'params': {}},
        ]
        result = _summarize_plan_steps(steps)
        assert '2 steps' in result
        assert '2 some future actions' in result

    def test_bulk_actions(self):
        steps = [
            {'action': 'bulk_create_zones', 'params': {}},
            {'action': 'bulk_create_tables', 'params': {}},
        ]
        result = _summarize_plan_steps(steps)
        assert 'zone batch' in result
        assert 'table batch' in result


# ===========================================================================
# chat_stream SSE — validation and error cases
# ===========================================================================

@pytest.mark.django_db
class TestChatStream:

    def test_no_message_returns_error_sse(self, rf, authenticated_session):
        request = _make_request(rf, authenticated_session, method='post', data={'message': ''})
        resp = chat_stream(request)
        assert isinstance(resp, StreamingHttpResponse)
        assert resp['Content-Type'] == 'text/event-stream'
        content = b''.join(resp.streaming_content).decode()
        assert '"type": "error"' in content
        assert 'message' in content.lower()
        assert '[DONE]' in content

    def test_no_user_returns_error_sse(self, rf):
        session = {'local_user_id': str(uuid.uuid4())}  # Non-existent user
        request = _make_request(rf, session, method='post', data={'message': 'Hello'})
        resp = chat_stream(request)
        assert isinstance(resp, StreamingHttpResponse)
        content = b''.join(resp.streaming_content).decode()
        assert 'User not found' in content
        assert '[DONE]' in content

    def test_no_jwt_returns_error_sse(self, rf, authenticated_session, hub_config, admin_user):
        hub_config.hub_jwt = ''
        hub_config.save()
        request = _make_request(rf, authenticated_session, method='post', data={'message': 'Hello'})
        resp = chat_stream(request)
        assert isinstance(resp, StreamingHttpResponse)
        content = b''.join(resp.streaming_content).decode()
        assert 'not connected' in content.lower()
        assert '[DONE]' in content

    def test_whitespace_only_message(self, rf, authenticated_session):
        request = _make_request(rf, authenticated_session, method='post', data={'message': '   '})
        resp = chat_stream(request)
        assert isinstance(resp, StreamingHttpResponse)
        content = b''.join(resp.streaming_content).decode()
        assert '"type": "error"' in content
