# AI Assistant

## Overview

| Property | Value |
|----------|-------|
| **Module ID** | `assistant` |
| **Version** | `1.0.0` |
| **Icon** | `sparkles-outline` |
| **Dependencies** | None |

## Models

### `AssistantConversation`

Tracks conversation state per user.

| Field | Type | Details |
|-------|------|---------|
| `user` | ForeignKey | → `accounts.LocalUser`, on_delete=CASCADE |
| `ai_conversation_id` | CharField | max_length=255, optional |
| `context` | CharField | max_length=50 |
| `title` | CharField | max_length=200, optional |
| `summary` | TextField | optional |
| `first_message` | TextField | optional |
| `message_count` | PositiveIntegerField |  |

### `AssistantActionLog`

Audit trail for all assistant-executed actions.

| Field | Type | Details |
|-------|------|---------|
| `user` | ForeignKey | → `accounts.LocalUser`, on_delete=CASCADE |
| `conversation` | ForeignKey | → `assistant.AssistantConversation`, on_delete=SET_NULL, optional |
| `tool_name` | CharField | max_length=100 |
| `tool_args` | JSONField |  |
| `result` | JSONField |  |
| `success` | BooleanField |  |
| `confirmed` | BooleanField |  |
| `error_message` | TextField | optional |

### `AssistantFeedback`

Tracks feedback events for product improvement.

Automatically recorded when tools fail, searches return zero results,
or users request features that don't exist. Sent to Cloud for
analysis and email notification to the ERPlora team.

| Field | Type | Details |
|-------|------|---------|
| `event_type` | CharField | max_length=30, choices: tool_error, zero_results, missing_feature |
| `tool_name` | CharField | max_length=100, optional |
| `user_message` | TextField | optional |
| `details` | JSONField |  |
| `user` | ForeignKey | → `accounts.LocalUser`, on_delete=CASCADE |
| `conversation` | ForeignKey | → `assistant.AssistantConversation`, on_delete=SET_NULL, optional |
| `action_log` | ForeignKey | → `assistant.AssistantActionLog`, on_delete=SET_NULL, optional |
| `sent_to_cloud` | BooleanField |  |
| `cloud_error` | CharField | max_length=255, optional |

## Cross-Module Relationships

| From | Field | To | on_delete | Nullable |
|------|-------|----|-----------|----------|
| `AssistantConversation` | `user` | `accounts.LocalUser` | CASCADE | No |
| `AssistantActionLog` | `user` | `accounts.LocalUser` | CASCADE | No |
| `AssistantActionLog` | `conversation` | `assistant.AssistantConversation` | SET_NULL | Yes |
| `AssistantFeedback` | `user` | `accounts.LocalUser` | CASCADE | No |
| `AssistantFeedback` | `conversation` | `assistant.AssistantConversation` | SET_NULL | Yes |
| `AssistantFeedback` | `action_log` | `assistant.AssistantActionLog` | SET_NULL | Yes |

## URL Endpoints

Base path: `/m/assistant/`

| Path | Name | Method |
|------|------|--------|
| `(root)` | `index` | GET |
| `chat/` | `chat` | GET |
| `history/` | `history` | GET |
| `logs/` | `logs` | GET |
| `chat/send/` | `chat_message` | GET |
| `poll/<str:request_id>/` | `poll_progress` | GET |
| `confirm/<str:log_id>/` | `confirm_action` | GET |
| `cancel/<str:log_id>/` | `cancel_action` | GET |

## Permissions

| Permission | Description |
|------------|-------------|
| `assistant.use_chat` | Use Chat |
| `assistant.use_setup_mode` | Use Setup Mode |
| `assistant.view_logs` | View Logs |
| `assistant.manage_settings` | Manage Settings |

**Role assignments:**

- **admin**: All permissions
- **manager**: `use_chat`, `use_setup_mode`, `view_logs`
- **employee**: `use_chat`, `use_setup_mode`, `view_logs`

## Navigation

| View | Icon | ID | Fullpage |
|------|------|----|----------|
| Chat | `chatbubbles-outline` | `chat` | No |
| History | `time-outline` | `history` | No |
| Action Log | `list-outline` | `logs` | No |

## File Structure

```
README.md
__init__.py
admin.py
api.py
apps.py
feedback.py
migrations/
  0001_initial.py
  __init__.py
models.py
module.py
prompts.py
static/
  assistant/
    js/
templates/
  assistant/
    pages/
      chat.html
      history.html
      logs.html
    partials/
      chat_modal.html
      chat_panel.html
      confirmation.html
      history_content.html
      logs_content.html
      message.html
      progress.html
tests/
  __init__.py
  conftest.py
  test_analytics.py
  test_feedback.py
  test_models.py
  test_prompts.py
  test_tools.py
  test_views.py
tools/
  __init__.py
  analytics_tools.py
  configure_tools.py
  hub_tools.py
  setup_tools.py
urls.py
views.py
```
