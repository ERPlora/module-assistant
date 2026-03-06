# AI Assistant Module

AI-powered business assistant for ERPlora Hub with contextual tool calling across all modules.

## Features

- Agentic chat with tool calling (280+ tools across 80 modules)
- Voice input support
- Conversation history and context management
- Action confirmation for write operations
- Anti-loop detection and JSON Schema validation for tool calls
- Automatic feedback collection (tool errors, zero results, missing features)
- Tiered subscription plans: Basic, Pro, Enterprise
- HTMX polling for streaming progress updates

## Installation

This module is installed automatically via the ERPlora Marketplace.

## Configuration

Requires an active assistant subscription tier configured in the Cloud portal.

## Usage

Access via: **Menu > AI Assistant**

### Views

| View | URL | Description |
|------|-----|-------------|
| Chat | `/m/assistant/` | Interactive chat interface |
| Chat (alias) | `/m/assistant/chat/` | Same as above |
| History | `/m/assistant/history/` | Browse and resume previous conversations |
| Action Log | `/m/assistant/logs/` | Audit trail of all assistant-executed actions |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/m/assistant/chat/send/` | POST | Send message to assistant (triggers agentic loop) |
| `/m/assistant/poll/<request_id>/` | GET | Poll for progress updates (HTMX polling) |
| `/m/assistant/confirm/<log_id>/` | POST | Confirm a pending write action |
| `/m/assistant/cancel/<log_id>/` | POST | Cancel a pending write action |

## Architecture

- **Tool discovery**: Auto-discovers `ai_tools.py` in every active module
- **Agentic loop**: Up to 10 iterations per request with anti-loop detection
- **Schema validation**: Validates tool arguments against JSON Schema before execution
- **Proxy**: Cloud proxy converts OpenAI format to Gemini API
- **Models**: Gemini 2.5 Flash (Basic/Pro), Gemini 2.5 Pro (Enterprise)

## Models

| Model | Description |
|-------|-------------|
| `AssistantConversation` | Conversation state per user (context, title, summary, message count) |
| `AssistantActionLog` | Audit trail for executed actions (tool name, args, result, confirmed status) |
| `AssistantFeedback` | Automatic feedback events (tool errors, zero results, missing features) |

## Permissions

| Permission | Description |
|------------|-------------|
| `assistant.use_chat` | Use the chat interface |
| `assistant.use_setup_mode` | Use setup mode for initial configuration |
| `assistant.view_logs` | View action log |
| `assistant.manage_settings` | Manage assistant settings |

## Dependencies

None (integrates with all active modules via tool discovery)

## License

MIT

## Author

ERPlora Team - support@erplora.com
