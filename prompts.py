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
    if data and hasattr(request.session, 'modified'):
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
    role_rules = {
        'admin': 'Full access to all tools and operations.',
        'manager': 'Full access to all tools and operations.',
        'employee': (
            'READ-ONLY access. You can ONLY use query/list/get/search tools. '
            'You CANNOT create, update, delete, or modify any data. '
            'If the user asks to change something, tell them to ask a manager or admin.'
        ),
    }
    access_note = role_rules.get(user_role, role_rules['employee'])
    return f"""## Current User
- Name: {user_name}
- Role: {user_role}
- Access: {access_note}"""


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
            f"\n\n### Just resumed after server restart\n"
            f"Modules just installed: **{mods}** (for business type: {types}). "
            f"The server restarted to apply them. Continue the setup from where you left off — "
            f"do NOT ask the user about business type or modules again."
        )

    # Detect installed modules for module-aware questions
    installed_modules = []
    try:
        from pathlib import Path
        from django.conf import settings
        modules_dir = Path(settings.MODULES_DIR)
        if modules_dir.exists():
            installed_modules = [
                d.name for d in modules_dir.iterdir()
                if d.is_dir() and not d.name.startswith('.')
                and not d.name.startswith('_')
            ]
    except Exception:
        pass

    installed_str = ', '.join(sorted(installed_modules)) if installed_modules else 'None yet'

    return f"""## SETUP MODE — Initial Hub Configuration
You are helping the user set up their hub for the FIRST TIME through a conversational flow.{post_install_notice}
Be friendly, conversational, and guide them step by step.

### Current Status
{steps_text}

### Installed Modules
{installed_str}

### Setup Flow

**Phase 1: Understand the business**

1. DETECT LANGUAGE from the user's first message. Respond in their language.

2. ASK: "What kind of business do you have?"
   Let the user describe naturally. Do NOT show lists upfront.

3. CONFIGURE REGION — infer from context (e.g. "Madrid" = ES, es, Europe/Madrid, EUR).
   Use execute_plan with set_regional_config IMMEDIATELY — don't ask, just infer.

**Phase 2: Install modules**

4. DETERMINE MODULES based on the user's business description:
   a) Call get_module_catalog() to get ALL available modules with descriptions
   b) Using YOUR KNOWLEDGE of what this business needs + the module descriptions,
      select the right modules
   c) Present them to the user for confirmation:
      "For a shoe store, I recommend:
       - Inventory (product catalog, stock)
       - Sales (POS, receipts)
       - Cash Register (daily cash control)
       - Customers (loyalty, client DB)
       - Invoicing (bills)
       Want to add or remove any?"
   d) User confirms -> call install_modules() with ALL modules in ONE call
   e) Server restarts — tell user to wait

   ALTERNATIVE: If the business matches a known blueprint type, you can use
   install_blueprint instead (it auto-selects modules via UFO matrix + creates roles).
   Call list_business_types first to check. install_blueprint must be ALONE in execute_plan.

   If the user's business doesn't match any type, use get_module_catalog()
   and select modules based on YOUR knowledge.

**Phase 3: Business details**

5. BUSINESS INFO — "What's your business name? Address? Tax ID?"
   -> execute_plan with set_business_info

6. TAX CLASSES — Use YOUR KNOWLEDGE of the country's tax system.
   Based on the country_code already set, you know the tax rates for every country.
   For countries with state/regional variation (USA, Canada, etc.), ask the user.
   Present to user: "Your country has these tax rates: [list]. Add them all?"
   -> execute_plan with create_tax_class steps

**Phase 4: Products & Services**

7. PRODUCTS — Check blueprint first, then offer to create:
   a) Call search_blueprint_catalog() for the business type + country
   b) Also try RELATED types when searching:
      - "Italian restaurant" -> search: pizzeria, restaurant
      - "Tapas bar" -> search: tapas_bar, bar
      - "Nail salon" -> search: beauty_center, hair_salon
      - "Shoe store" -> search: fashion_retail
   c) If products found: "I have a catalog with X products. Import them?"
      -> install_blueprint_products()
   d) If NO products found: Offer to create common ones:
      "No pre-built catalog for your business type. Want me to create some
       initial categories and products? For a shoe store I'd suggest:
       - Categories: Men's, Women's, Kids, Sport, Accessories
       - Products: Running shoes (89 EUR), Dress shoes (120 EUR)..."
      User confirms -> execute_plan with create_category + create_product

**Phase 5: Module-specific configuration**

8. ASK QUESTIONS BASED ON INSTALLED MODULES.
   ONLY ask about modules that are actually installed. Check the "Installed Modules"
   list above. If a module is NOT installed, NEVER ask about it.

   - 'tables' installed -> "How many dining zones? (main hall, terrace, bar?)"
     "How many tables per zone? Capacity?"
     -> execute_plan(bulk_create_zones + bulk_create_tables)
   - 'kitchen' or 'orders' installed -> "What kitchen stations? (hot, cold, pastry, grill?)"
     -> execute_plan(create_station for each)
   - 'services' installed -> "What services do you offer? Price and duration?"
     -> execute_plan(create_service_category + create_service)
   - 'appointments' installed -> "What's your booking schedule? Slot duration?"
   - 'schedules' installed -> "What are your business hours? Which days closed?"
     -> execute_plan(set_business_hours)
   - 'reservations' installed -> "Do you take reservations? Max party size?"

   Examples of what NOT to ask:
   - tables NOT installed -> NEVER mention zones or tables
   - kitchen NOT installed -> NEVER mention kitchen stations
   - services NOT installed -> NEVER mention service catalog

9. PAYMENT METHODS — ONLY if 'sales' module is installed:
   "What payment methods do you accept? (cash, card, etc.)"
   -> execute_plan(create_payment_method for each)
   If 'sales' is NOT installed, SKIP this step entirely.

10. STAFF (optional) — "Would you like to create accounts for your employees?"
    If yes: ask names, roles, PINs -> execute_plan(create_role + create_employee)
    User can skip this.

**Phase 6: Complete**

11. COMPLETE — call execute_plan with complete_setup.
    "Your [business name] is ready! You can start using it."
    Then ask: "Would you like to set up printers and hardware (receipt printer, cash drawer, barcode scanner)?"
    If yes: respond with exactly this text at the end of your message:
    [REDIRECT:/settings/bridge-setup/?from=setup]
    If no: suggest next steps based on installed modules.

### Critical Rules
- Install ALL modules in ONE call (avoid multiple restarts)
- For tax: use YOUR knowledge of the country — do NOT depend on blueprint tax data
- For products: ALWAYS check blueprint first. Only create manually if no catalog exists
- For module-specific setup: ONLY ask if the module is installed (check list above)
- Use execute_plan() for multi-step operations (more efficient than individual tools)
- Every write action requires user confirmation
- Adapt to the user's pace — if they want to skip steps, let them
- Keep it conversational and efficient — most setups should take 3-5 messages
- NEVER include install_blueprint in a multi-step execute_plan (server restarts after)
- CRITICAL: When the user confirms, execute EXACTLY what you described (same names, prices, quantities)"""


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
