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


def _consume_post_install(request):
    """Read and clear the post-install session flag set before server restart."""
    data = request.session.pop('assistant_post_install', None)
    if data:
        request.session.modified = True
    return data


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
    post_install = _consume_post_install(request)

    parts = [
        _base_instructions(hub_config.language),
        _user_context(user_name, user_role),
        _store_context(store_config, hub_config),
        _business_context(hub_config),
        _datetime_context(hub_config),
        _modules_context(module_entries),
        _tools_context(context, request),
        _data_overview(),
        _roles_context(request),
        _tax_context(),
        _payment_context(),
        _memories_context(hub_config),
        _recent_activity(user_id),
        _conversation_history(user_id),
    ]

    if context == 'setup':
        parts.append(_setup_context(hub_config, store_config, post_install))

    parts.append(_safety_rules())

    # Filter out empty parts
    return '\n\n'.join(p for p in parts if p)


def _base_instructions(language):
    lang_name = {
        'en': 'English', 'es': 'Spanish', 'de': 'German',
        'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese',
    }.get(language, 'English')

    return f"""You are an AI assistant for ERPlora (modular POS/ERP).
Respond in the language the user writes in (hub language: {lang_name}, but user language takes priority).
Be concise and proactive. Use tools for all actions.

## Tool Loading
Core tools always available. For module-specific work, call `load_module_tools(["module_id"])` first — deps resolved automatically.
Unload with `unload_module_tools` when switching context. Loaded tools persist across messages."""


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


def _business_context(hub_config):
    """
    Inject business type, sector, and UFO functional units from Blueprint API.
    Data is fetched dynamically (cached 5min in Redis) so it reflects
    any JSON changes in the blueprints repo without code changes.
    """
    type_codes = getattr(hub_config, 'selected_business_types', None) or []
    if not type_codes:
        return ''

    try:
        from apps.core.services.blueprint_service import BlueprintService

        language = hub_config.language or 'en'
        lines = ['## Business Context']
        lines.append(f"- Business types: {', '.join(type_codes)}")

        # Fetch detail for each type (cached, fast)
        sector = None
        ufo_essential = []
        ufo_recommended = []
        key_modules = []

        for code in type_codes:
            detail = BlueprintService.get_type_detail(code, language=language)
            if not detail:
                continue

            # Sector (use first type's sector)
            if not sector and detail.get('sector'):
                sector = detail['sector']

            # UFO matrix — collect essential/recommended units
            ufo = detail.get('ufo', {})
            for unit_code, level in ufo.items():
                if level == 'essential' and unit_code not in ufo_essential:
                    ufo_essential.append(unit_code)
                elif level == 'recommended' and unit_code not in ufo_recommended:
                    ufo_recommended.append(unit_code)

            # Key modules for this type
            for mod in detail.get('modules', []):
                mid = mod if isinstance(mod, str) else mod.get('id', '')
                if mid and mid not in key_modules:
                    key_modules.append(mid)

        if sector:
            lines.append(f"- Sector: {sector}")
        if ufo_essential:
            lines.append(f"- Essential functional units: {', '.join(ufo_essential)}")
        if ufo_recommended:
            lines.append(f"- Recommended functional units: {', '.join(ufo_recommended)}")
        if key_modules:
            lines.append(f"- Key modules for this business: {', '.join(key_modules[:20])}")

        return '\n'.join(lines)

    except Exception as e:
        logger.debug(f"[ASSISTANT] Error building business context: {e}")
        # Fallback: at least show the type codes without API call
        return f"## Business Context\n- Business types: {', '.join(type_codes)}"


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
        return "## Active Modules\nNone. Use `get_module_catalog` to install modules."

    ids = [mid for mid, _, _ in module_entries]
    return f"## Active Modules ({len(module_entries)})\n{', '.join(ids)}"


def _load_module_context(module_id: str) -> str:
    """
    Load ai_context.py from a module and return its CONTEXT string.
    Returns empty string if the module has no ai_context.py or CONTEXT variable.
    Fast: no network call, pure file import.
    """
    try:
        import importlib
        mod = importlib.import_module(f'{module_id}.ai_context')
        ctx = getattr(mod, 'CONTEXT', '')
        return ctx.strip() if ctx else ''
    except ImportError:
        return ''
    except Exception as e:
        logger.debug(f"[ASSISTANT] Error loading ai_context for {module_id}: {e}")
        return ''


def _tools_context(context, request):
    """Summarize available core tools and inject context for loaded modules."""
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

        # Get currently loaded modules from session
        loaded_modules = set(request.session.get('assistant_loaded_modules', []))

        # Only show core tools + loaded module tools
        tools = get_tools_for_context(context, user, loaded_modules=loaded_modules)
        if not tools:
            return ''

        # Group by module using the registry
        by_module = {}
        for schema in tools:
            tool_name = schema.get('name', '')
            tool_inst = TOOL_REGISTRY.get(tool_name)
            mod = getattr(tool_inst, 'module_id', None) or 'hub_core'
            by_module.setdefault(mod, []).append(tool_inst)

        # Compact: just tool names grouped by module, mark writes with *
        core_names = []
        module_names = []
        for mod in sorted(by_module.keys()):
            for t in by_module[mod]:
                entry = f"{t.name}{'*' if t.requires_confirmation else ''}"
                if mod == 'hub_core':
                    core_names.append(entry)
                else:
                    module_names.append(f"{entry}[{mod}]")

        lines = [f"## Core Tools ({len(tools)}, *=requires confirmation)"]
        if core_names:
            lines.append(', '.join(core_names))
        if module_names:
            lines.append('Loaded module tools: ' + ', '.join(module_names))
        lines.append("Use `load_module_tools([id])` to load module tools. `unload_module_tools` to free context.")

        # Inject ai_context.py knowledge for each loaded module
        module_contexts = []
        for mid in sorted(loaded_modules):
            ctx = _load_module_context(mid)
            if ctx:
                module_contexts.append(ctx)
        if module_contexts:
            lines.append('\n' + '\n\n'.join(module_contexts))

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


def _memories_context(hub_config):
    """Inject persistent memories saved by the AI across previous sessions."""
    try:
        from assistant.models import AssistantMemory

        hub_id = hub_config.hub_id
        if not hub_id:
            return ''

        memories = AssistantMemory.objects.filter(
            hub_id=hub_id,
            is_deleted=False,
        ).order_by('key')

        if not memories.exists():
            return ''

        lines = ['## Your Memories (from previous sessions)']
        for m in memories:
            lines.append(f"- {m.key}: {m.content}")

        return '\n'.join(lines)
    except Exception as e:
        logger.debug(f"[ASSISTANT] Error loading memories: {e}")
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
            'list_business_types', 'get_selected_business_types', 'list_roles',
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
            date_str = conv.updated_at.strftime('%m-%d %H:%M')
            summary = conv.summary[:100] if conv.summary else ''
            lines.append(f"- {date_str}: {summary}")

        return '\n'.join(lines)
    except Exception:
        return ''


def _setup_context(hub_config, store_config, post_install=None):
    """Build setup context for AI-driven initial configuration."""
    # Determine what's already configured
    has_region = bool(hub_config.language and hub_config.country_code)
    has_business_type = bool(hub_config.selected_business_types)
    has_business_info = bool(store_config.business_name)
    has_tax = bool(store_config.is_configured)

    steps = []
    if has_region:
        steps.append(f"1. Regional: DONE ({hub_config.country_code}, {hub_config.language}, {hub_config.currency})")
    else:
        steps.append("1. Regional: PENDING")

    if has_business_type:
        types = ', '.join(hub_config.selected_business_types)
        steps.append(f"2. Business type: DONE ({types})")
    else:
        steps.append("2. Business type: PENDING")

    if has_business_info:
        steps.append(f"3. Business info: DONE ({store_config.business_name})")
    else:
        steps.append("3. Business info: PENDING")

    if has_tax:
        steps.append("4. Tax: DONE")
    else:
        steps.append("4. Tax: PENDING")

    steps_text = '\n'.join(f"- {s}" for s in steps)

    post_install_notice = ''
    if post_install:
        mods = ', '.join(post_install.get('modules_installed', [])) or 'several'
        types = ', '.join(post_install.get('type_codes', []))
        post_install_notice = (
            f"\n\n### ⚡ Just resumed after server restart\n"
            f"Modules just installed: **{mods}** (for business type: {types}). "
            f"The server restarted to apply them. Continue the setup from where you left off — "
            f"do NOT ask the user about business type or modules again."
        )

    return f"""## SETUP MODE — Initial Hub Configuration
You are helping the user set up their hub for the FIRST TIME through a conversational flow.{post_install_notice}
This replaces the traditional setup wizard. Be friendly, conversational, and guide them step by step.

### Current Status
{steps_text}

### Setup Flow (follow this order)

**Step 1: Detect language and configure region.**
CRITICAL: Detect the user's language from their message. If they write in Spanish, set language=es.
If they mention a location (e.g., "Madrid", "España"), infer the country, timezone, and currency.
Use ExecutePlan with `set_regional_config` IMMEDIATELY — don't ask, just infer from context:
- country_code (ISO 2-letter: ES, FR, DE, US, GB, MX, etc.)
- language (detected from the user's message: es, en, fr, de, it, pt)
- timezone (Europe/Madrid, America/New_York, etc.)
- currency (EUR, USD, GBP, MXN, etc.)
Do this as your FIRST action, before responding with the plan.

**Step 2: Ask what kind of business they have (or infer from their message).**
Use `list_business_types` to show available business types (optionally filtered by sector).
Then use ExecutePlan with `install_blueprint` action:
- params: {{"type_codes": ["restaurant"], "sector": "hospitality"}}
This installs the essential modules, creates roles, compliance modules (per country), and tax presets.
IMPORTANT: After install_blueprint, the hub will have new modules available. Tell the user what was installed.

**Step 3: Install product catalog.**
Use `install_blueprint_products` to install pre-built products with images from the blueprint catalog.
- params: {{"product_codes": ["*"]}} to install all, or list specific codes
- First use `search_blueprint_catalog` to show what's available
- Products come with images, prices, categories, and correct tax classes
- PREFER this over manually creating products with `create_product`

**Step 4: Ask for business details.**
Use ExecutePlan with `set_business_info`:
- business_name, business_address, vat_number, phone, email

**Step 5: Complete setup.**
Use ExecutePlan with `complete_setup` to mark the hub as configured.
Then congratulate the user and suggest next steps (configure tables, create employees, start selling).

### Product Management Guidelines
- When the user asks to add products, FIRST check if a blueprint catalog exists with `list_available_catalogs`
- If a catalog exists, suggest using `install_blueprint_products` instead of manual creation
- Products from the catalog come with images, correct tax classes, and realistic prices
- For custom products not in the catalog, use `create_product` via ExecutePlan
- Users can export products to CSV with `export_products_csv` and import with `import_products_csv`

### Guidelines
- ALWAYS respond in the same language the user writes in
- If the user provides all info in one message, execute ALL steps without asking — don't make them wait
- Use country defaults when possible (Spain → EUR, Europe/Madrid, es)
- If the user says "restaurant in Madrid", you can infer: ES, es, Europe/Madrid, EUR, restaurant, hospitality
- After blueprint install, briefly list what modules were installed
- The user can always go to /setup/ for the manual wizard instead
- Keep it conversational and efficient — most setups should take 2-3 messages"""


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
