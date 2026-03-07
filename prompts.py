"""
System prompt builder for the AI Assistant.

Builds a dynamic system prompt per request including:
- User info (name, role, permissions)
- Store info (name, currency, language, tax)
- Current date/time/timezone
- Active modules with their available tools
- Data overview (counts of products, customers, sales, etc.)
- Configured roles and tax classes
- Payment methods
- Recent activity from action logs
- Conversation history summaries
- Setup state (if in setup context)
- Safety rules
"""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


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

    user_id = request.session.get('local_user_id')
    user_name = request.session.get('user_name', 'User')
    user_role = request.session.get('user_role', 'employee')

    # Get active modules with descriptions
    loader = ModuleLoader()
    menu_items = loader.get_menu_items()
    module_entries = _collect_module_info(menu_items)

    # Build prompt parts
    parts = [
        _base_instructions(hub_config.language),
        _user_context(user_name, user_role),
        _store_context(store_config, hub_config),
        _datetime_context(hub_config),
        _modules_context(module_entries),
        _tools_context(context, request),
        _data_overview(),
        _roles_context(request),
        _tax_context(),
        _payment_context(),
        _recent_activity(user_id),
        _conversation_history(user_id),
    ]

    if context == 'setup':
        parts.append(_setup_context(hub_config, store_config))

    parts.append(_safety_rules())

    # Filter out empty parts
    return '\n\n'.join(p for p in parts if p)


def _base_instructions(language):
    lang_name = {
        'en': 'English', 'es': 'Spanish', 'de': 'German',
        'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese',
    }.get(language, 'English')

    return f"""You are an AI assistant for ERPlora, a modular POS/ERP system.
You help users configure their hub, manage products, employees, and business operations.

IMPORTANT: Always respond in {lang_name} (the user's configured language).
Be concise, helpful, and proactive. Suggest next steps when appropriate.
When you need to perform an action, use the available tools.

## Module Recommendations
When a user describes their business or asks which modules they need:
1. Use the `get_module_catalog` tool to fetch ALL available modules with descriptions, functions, and industries
2. Based on the user's business type, recommend the most relevant modules
3. Indicate which modules are already installed vs need to be added
4. Mention dependencies (if module A requires module B)
5. Explain pricing: free modules can be installed directly, paid modules need to be purchased from the marketplace

You can also use `list_available_blocks` to see functional blocks (pre-configured bundles of modules for common business types like retail, hospitality, services, etc.)."""


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


def _datetime_context(hub_config):
    """Current date, time, and timezone."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(hub_config.timezone or 'UTC')
    except Exception:
        import pytz
        tz = pytz.timezone(hub_config.timezone or 'UTC')

    now = datetime.now(tz)
    day_name = now.strftime('%A')

    return f"""## Current Date/Time
{now.strftime('%Y-%m-%d %H:%M')} ({hub_config.timezone or 'UTC'}, {day_name})"""


def _collect_module_info(menu_items):
    """Collect module IDs, names, and descriptions from module.py files."""
    import importlib
    entries = []
    for item in menu_items:
        mid = item.get('module_id', '')
        label = str(item.get('label', mid))
        desc = ''
        try:
            mod = importlib.import_module(f"{mid}.module")
            d = getattr(mod, 'MODULE_DESCRIPTION', None)
            if d:
                desc = str(d)
        except Exception:
            pass
        entries.append((mid, label, desc))
    return entries


def _modules_context(module_entries):
    if not module_entries:
        return """## Active Modules
No modules installed yet. Use `get_module_catalog` to browse available modules."""

    lines = []
    for mid, label, desc in module_entries:
        if desc:
            lines.append(f"- **{label}** ({mid}): {desc}")
        else:
            lines.append(f"- **{label}** ({mid})")

    return f"""## Active Modules ({len(module_entries)} installed)
{chr(10).join(lines)}

Use `get_module_catalog` to see all available modules (including those not yet installed)."""


def _tools_context(context, request):
    """Summarize available tools grouped by module."""
    try:
        from assistant.tools import get_tools_for_context, TOOL_REGISTRY
        from apps.accounts.models import LocalUser

        user_id = request.session.get('local_user_id')
        user = None
        if user_id:
            try:
                user = LocalUser.objects.get(id=user_id)
            except LocalUser.DoesNotExist:
                pass

        tools = get_tools_for_context(context, user)
        if not tools:
            return ''

        # Group by module using the registry
        by_module = {}
        for schema in tools:
            tool_name = schema.get('name', '')
            tool_inst = TOOL_REGISTRY.get(tool_name)
            mod = getattr(tool_inst, 'module_id', None) or 'hub_core'
            by_module.setdefault(mod, []).append(tool_inst)

        lines = [f"## Available Tools ({len(tools)} total)"]
        for mod in sorted(by_module.keys()):
            mod_tools = by_module[mod]
            names = []
            for t in mod_tools:
                suffix = ' (write)' if t.requires_confirmation else ''
                names.append(f"{t.name}{suffix}")
            lines.append(f"- **{mod}**: {', '.join(names)}")

        return '\n'.join(lines)
    except Exception as e:
        logger.debug(f"[ASSISTANT] Error building tools context: {e}")
        return ''


def _data_overview():
    """Quick aggregate counts from active modules."""
    counts = {}

    try:
        from inventory.models import Product
        counts['products'] = Product.objects.filter(is_active=True).count()
    except Exception:
        pass

    try:
        from customers.models import Customer
        counts['customers'] = Customer.objects.filter(is_active=True).count()
    except Exception:
        pass

    try:
        from services.models import Service
        counts['services'] = Service.objects.filter(is_active=True).count()
    except Exception:
        pass

    try:
        from sales.models import Sale
        from datetime import date
        today = date.today()
        counts['sales_today'] = Sale.objects.filter(
            status='completed', created_at__date=today,
        ).count()
        counts['sales_this_month'] = Sale.objects.filter(
            status='completed', created_at__date__gte=today.replace(day=1),
        ).count()
    except Exception:
        pass

    try:
        from apps.accounts.models import LocalUser
        counts['employees'] = LocalUser.objects.filter(
            is_active=True, is_deleted=False,
        ).count()
    except Exception:
        pass

    if not counts:
        return ''

    lines = ['## Data Overview']
    for key, val in counts.items():
        label = key.replace('_', ' ').title()
        lines.append(f"- {label}: {val}")

    return '\n'.join(lines)


def _roles_context(request):
    """List configured roles with their permission wildcards."""
    try:
        from apps.accounts.models import Role, RolePermission
        hub_id = request.session.get('hub_id')
        roles = Role.objects.filter(
            hub_id=hub_id, is_active=True, is_deleted=False,
        ).order_by('source', 'name')

        if not roles.exists():
            return ''

        lines = ['## Configured Roles']
        for r in roles:
            wildcards = list(
                RolePermission.objects.filter(
                    role=r, is_deleted=False, wildcard__gt='',
                ).values_list('wildcard', flat=True)
            )
            perm_count = len(r.get_all_permissions()) if hasattr(r, 'get_all_permissions') else 0
            wc_str = ', '.join(wildcards[:10]) if wildcards else 'none'
            lines.append(
                f"- **{r.display_name}** ({r.name}, {r.source}): "
                f"{perm_count} permissions, wildcards: {wc_str}"
            )

        lines.append(
            "\nRoles define what each user can do. "
            "admin=full access, manager=CRUD without delete/settings, "
            "employee=view+basic ops, viewer=read-only. "
            "Custom roles can be created by admins."
        )
        return '\n'.join(lines)
    except Exception:
        return ''


def _tax_context():
    """List configured tax classes."""
    try:
        from apps.configuration.models import TaxClass
        classes = TaxClass.objects.all().order_by('-is_default', 'name')

        if not classes.exists():
            return ''

        parts = []
        for tc in classes:
            default = ' (default)' if tc.is_default else ''
            parts.append(f"{tc.name} {tc.rate}%{default}")

        return f"## Tax Classes\n{', '.join(parts)}"
    except Exception:
        return ''


def _payment_context():
    """List configured payment methods."""
    try:
        from sales.models import PaymentMethod
        methods = PaymentMethod.objects.filter(is_active=True).order_by('sort_order')

        if not methods.exists():
            return ''

        parts = []
        for pm in methods:
            parts.append(f"{pm.name} ({pm.type})")

        return f"## Payment Methods\n{', '.join(parts)}"
    except Exception:
        return ''


def _recent_activity(user_id):
    """Last 5 meaningful tool actions for this user."""
    if not user_id:
        return ''

    try:
        from assistant.models import AssistantActionLog

        # Skip read-only discovery tools
        skip_tools = {
            'get_hub_config', 'get_store_config', 'list_modules',
            'list_available_blocks', 'get_selected_blocks', 'list_roles',
            'list_tax_classes', 'list_employees', 'get_module_catalog',
        }

        recent = AssistantActionLog.objects.filter(
            user_id=user_id, success=True,
        ).order_by('-created_at')[:20]

        actions = []
        for log in recent:
            if log.tool_name in skip_tools:
                continue
            # Build a concise description
            key_arg = ''
            for k in ('name', 'title', 'business_name', 'module_id', 'query'):
                val = log.tool_args.get(k)
                if val:
                    key_arg = f" '{val}'"
                    break
            actions.append(f"{log.tool_name}{key_arg}")
            if len(actions) >= 5:
                break

        if not actions:
            return ''

        return f"## Recent Activity\n{', '.join(actions)}"
    except Exception:
        return ''


def _conversation_history(user_id):
    """Include recent conversation summaries for contextual continuity."""
    if not user_id:
        return ''

    try:
        from assistant.models import AssistantConversation
        recent = AssistantConversation.objects.filter(
            user_id=user_id,
        ).exclude(summary='').exclude(summary__isnull=True).order_by('-updated_at')[:5]

        if not recent:
            return ''

        lines = ['## Recent Conversations']
        for conv in recent:
            date_str = conv.updated_at.strftime('%Y-%m-%d %H:%M')
            title = conv.title or conv.first_message[:50] if hasattr(conv, 'first_message') and conv.first_message else 'Untitled'
            summary = conv.summary[:150] if conv.summary else ''
            lines.append(f"- [{date_str}] {title}: {summary}")

        return '\n'.join(lines)
    except Exception:
        return ''


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
7. If an operation fails, explain what went wrong and suggest alternatives
8. CRITICAL: When the user confirms a plan you presented, execute EXACTLY what you described.
   Use the same names, prices, quantities, and details. Never substitute generic data for specific details."""
