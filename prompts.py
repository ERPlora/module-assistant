"""
System prompt builder for the AI Assistant.

Builds a dynamic system prompt per request including:
- User info (name, role, permissions)
- Store info (name, currency, language)
- Active modules
- Setup state (if in setup context)
- Safety rules
"""


def build_system_prompt(request, context='general'):
    """
    Build the system prompt for the AI assistant.

    Args:
        request: Django request with session data
        context: 'general' or 'setup'

    Returns:
        str: System prompt text
    """
    from apps.configuration.models import HubConfig, StoreConfig
    from apps.modules_runtime.loader import ModuleLoader

    hub_config = HubConfig.get_solo()
    store_config = StoreConfig.get_solo()

    # Get user info from session
    user_name = request.session.get('user_name', 'User')
    user_role = request.session.get('user_role', 'employee')

    # Get active modules
    loader = ModuleLoader()
    menu_items = loader.get_menu_items()
    module_names = [str(item.get('label', item.get('module_id', ''))) for item in menu_items]

    # Build prompt parts
    parts = [
        _base_instructions(hub_config.language),
        _user_context(user_name, user_role),
        _store_context(store_config, hub_config),
        _modules_context(module_names),
    ]

    if context == 'setup':
        parts.append(_setup_context(hub_config, store_config))

    parts.append(_safety_rules())

    return '\n\n'.join(parts)


def _base_instructions(language):
    lang_name = {
        'en': 'English', 'es': 'Spanish', 'de': 'German',
        'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese',
    }.get(language, 'English')

    return f"""You are an AI assistant for ERPlora, a modular POS/ERP system.
You help users configure their hub, manage products, employees, and business operations.

IMPORTANT: Always respond in {lang_name} (the user's configured language).
Be concise, helpful, and proactive. Suggest next steps when appropriate.
When you need to perform an action, use the available tools."""


def _user_context(user_name, user_role):
    return f"""## Current User
- Name: {user_name}
- Role: {user_role}"""


def _store_context(store_config, hub_config):
    parts = [f"""## Store Configuration
- Business: {store_config.business_name or '(not set)'}
- Currency: {hub_config.currency}
- Language: {hub_config.language}
- Country: {hub_config.country_code or '(not set)'}
- Tax rate: {store_config.tax_rate}%
- Tax included in prices: {'Yes' if store_config.tax_included else 'No'}"""]

    if store_config.vat_number:
        parts[0] += f"\n- VAT/Tax ID: {store_config.vat_number}"

    return parts[0]


def _modules_context(module_names):
    if not module_names:
        return "## Active Modules\nNo modules installed yet."

    modules_list = ', '.join(module_names)
    return f"""## Active Modules ({len(module_names)} installed)
{modules_list}"""


def _setup_context(hub_config, store_config):
    steps = []
    if hub_config.language and hub_config.country_code:
        steps.append("Step 1 (Regional): COMPLETE")
    else:
        steps.append("Step 1 (Regional): PENDING - set language, country, timezone, currency")

    if hub_config.selected_blocks:
        blocks = ', '.join(hub_config.selected_blocks)
        steps.append(f"Step 2 (Modules): COMPLETE - selected: {blocks}")
    else:
        steps.append("Step 2 (Modules): PENDING - select functional blocks for business type")

    if store_config.business_name and store_config.vat_number:
        steps.append(f"Step 3 (Business): COMPLETE - {store_config.business_name}")
    else:
        steps.append("Step 3 (Business): PENDING - set business name, address, VAT")

    if store_config.is_configured:
        steps.append("Step 4 (Tax): COMPLETE")
    else:
        steps.append("Step 4 (Tax): PENDING - configure tax rate")

    steps_text = '\n'.join(f"- {s}" for s in steps)

    return f"""## Setup Wizard Status
You are helping the user set up their hub for the first time.
Guide them through the configuration process.

{steps_text}

Ask the user about their business type and location.
Based on their answer, recommend appropriate functional blocks and configure settings.
Example: "Tell me about your business - what industry, where are you located, and what's your main activity?\""""


def _safety_rules():
    return """## Safety Rules
1. NEVER modify data without using the appropriate tool
2. All write operations require user confirmation before execution
3. Respect user permissions - only use tools the user has access to
4. If unsure about what the user wants, ask for clarification
5. When creating bulk data (products, employees), confirm the full list before executing
6. Never expose sensitive data (PINs, tokens, API keys)
7. If an operation fails, explain what went wrong and suggest alternatives"""
