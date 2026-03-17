"""
P0 Hub Core Tools — always available.

These tools operate on the Hub's core configuration: HubConfig, StoreConfig,
TaxClass, modules, roles, and employees.
"""
import logging

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


# ============================================================================
# READ TOOLS
# ============================================================================

@register_tool
class GetHubConfig(AssistantTool):
    name = "get_hub_config"
    description = "Get current hub configuration: language, currency, timezone, country, theme, dark mode"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig
        config = HubConfig.get_solo()
        return {
            "language": config.language,
            "currency": config.currency,
            "timezone": config.timezone,
            "country_code": config.country_code,
            "color_theme": config.color_theme,
            "dark_mode": config.dark_mode,
            "is_configured": config.is_configured,
        }


@register_tool
class GetStoreConfig(AssistantTool):
    name = "get_store_config"
    description = "Get store/business configuration: name, address, VAT, phone, email, tax settings"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        return {
            "business_name": store.business_name,
            "business_address": store.business_address,
            "vat_number": store.vat_number,
            "phone": store.phone,
            "email": store.email,
            "website": store.website,
            "tax_rate": str(store.tax_rate),
            "tax_included": store.tax_included,
            "is_configured": store.is_configured,
        }


@register_tool
class ListBusinessTypes(AssistantTool):
    name = "list_business_types"
    description = (
        "List available business types from the Blueprint system, grouped by sector. "
        "Use this to help the user choose their business type during setup. "
        "Optionally filter by sector code (e.g., 'hospitality', 'retail', 'personal_services')."
    )
    short_description = "List business types from Blueprint catalog, grouped by sector. Filter by sector code."
    parameters = {
        "type": "object",
        "properties": {
            "sector": {
                "type": "string",
                "description": "Optional sector code to filter (e.g., 'hospitality'). If empty, returns all sectors with their types.",
            },
            "language": {
                "type": "string",
                "description": "Language code for translations (e.g., 'es', 'en'). Defaults to hub language.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.core.services.blueprint_service import BlueprintService
        from apps.configuration.models import HubConfig

        hub_config = HubConfig.get_solo()
        language = args.get('language') or hub_config.language or 'en'
        sector_filter = args.get('sector', '')

        try:
            sectors_data = BlueprintService.get_sectors(language=language)
            sectors = sectors_data.get('sectors', []) if isinstance(sectors_data, dict) else sectors_data

            result = []
            for sector in sectors:
                sector_code = sector.get('code', '') if isinstance(sector, dict) else sector
                sector_name = sector.get('name', sector_code) if isinstance(sector, dict) else sector_code

                if sector_filter and sector_code != sector_filter:
                    continue

                types_data = BlueprintService.get_types(sector=sector_code, language=language)
                types_list = types_data if isinstance(types_data, list) else []

                result.append({
                    "sector": sector_code,
                    "sector_name": sector_name,
                    "types": [
                        {
                            "code": t.get("code", ""),
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                        }
                        for t in types_list
                    ],
                })

            return {"sectors": result, "total_types": sum(len(s["types"]) for s in result)}
        except Exception as e:
            return {"error": f"Failed to fetch business types: {str(e)}"}


@register_tool
class GetSelectedBusinessTypes(AssistantTool):
    name = "get_selected_business_types"
    description = "Get the business types currently selected for this hub"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig
        config = HubConfig.get_solo()
        return {
            "selected_business_types": config.selected_business_types or [],
            "business_sector": config.business_sector or '',
        }


@register_tool
class ListModules(AssistantTool):
    name = "list_modules"
    description = "List all installed and active modules on this hub, including what each module does"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import importlib
        from apps.modules_runtime.loader import ModuleLoader
        loader = ModuleLoader()
        menu_items = loader.get_menu_items()
        modules = []
        for item in menu_items:
            mid = item.get("module_id", "")
            entry = {
                "module_id": mid,
                "label": str(item.get("label", "")),
                "icon": item.get("icon", ""),
            }
            try:
                mod = importlib.import_module(f"{mid}.module")
                desc = getattr(mod, 'MODULE_DESCRIPTION', None)
                if desc:
                    entry["description"] = str(desc)
                nav = getattr(mod, 'NAVIGATION', [])
                if nav:
                    entry["pages"] = [str(n.get('label', '')) for n in nav]
            except Exception:
                pass
            modules.append(entry)
        return {"modules": modules, "total": len(modules)}


@register_tool
class GetModuleCatalog(AssistantTool):
    name = "get_module_catalog"
    description = (
        "Get the full module catalog from the Cloud marketplace. "
        "Returns ALL available modules with descriptions, business functions, "
        "industries, pricing, and dependencies. Use this tool when the user "
        "asks what modules to install for their business type, or wants to "
        "know what modules are available and what they do."
    )
    short_description = "Get all available modules from marketplace with functions, pricing, dependencies. Use before install_modules."
    parameters = {
        "type": "object",
        "properties": {
            "business_type": {
                "type": "string",
                "description": "Optional: filter description for the user's business type (e.g., 'restaurant', 'retail', 'clinic'). If provided, the AI should use this to prioritize recommendations from the results.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import requests as http_requests
        from pathlib import Path
        from django.conf import settings

        base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com')

        modules_dir = Path(settings.MODULES_DIR)
        installed_ids = set()
        if modules_dir.exists():
            for d in modules_dir.iterdir():
                if d.is_dir() and not d.name.startswith('.'):
                    installed_ids.add(d.name.lstrip('_'))

        from apps.configuration.models import HubConfig
        hub_config = HubConfig.get_solo()
        auth_token = hub_config.hub_jwt or hub_config.cloud_api_token
        headers = {'Accept': 'application/json'}
        if auth_token:
            headers['X-Hub-Token'] = auth_token

        try:
            resp = http_requests.get(
                f"{base_url}/api/marketplace/modules/",
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                return {"error": f"Cloud API returned {resp.status_code}"}

            data = resp.json()
            modules_list = data.get('results', data) if isinstance(data, dict) else data
            if not isinstance(modules_list, list):
                return {"error": "Unexpected API response format"}

            catalog = []
            for m in modules_list:
                mid = m.get('module_id', '')
                entry = {
                    "module_id": mid,
                    "name": m.get('name', ''),
                    "description": m.get('description', ''),
                    "functional_unit": m.get('functional_unit', ''),
                    "sector": m.get('sector', ''),
                    "business_types": m.get('business_types', []),
                    "functions": m.get('functions_names', []),
                    "module_type": m.get('module_type', ''),
                    "price": str(m.get('price', 0)),
                    "is_installed": mid in installed_ids or m.get('slug', '') in installed_ids,
                    "is_owned": m.get('is_owned', False),
                    "dependency_ids": m.get('dependency_ids', []),
                }
                catalog.append(entry)

            return {
                "modules": catalog,
                "total": len(catalog),
                "installed_count": sum(1 for c in catalog if c['is_installed']),
            }

        except Exception as e:
            return {"error": f"Failed to fetch catalog: {str(e)}"}


@register_tool
class ListRoles(AssistantTool):
    name = "list_roles"
    description = "List all roles (basic, solution, custom) with their permission wildcards and expanded permission count"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import Role, RolePermission
        hub_id = request.session.get('hub_id')
        roles = Role.objects.filter(
            hub_id=hub_id, is_deleted=False, is_active=True
        ).order_by('source', 'name')

        result = []
        for r in roles:
            wildcards = list(
                RolePermission.objects.filter(
                    role=r, is_deleted=False, wildcard__gt='',
                ).values_list('wildcard', flat=True)
            )
            expanded = r.get_all_permissions() if hasattr(r, 'get_all_permissions') else set()
            result.append({
                "id": str(r.id),
                "name": r.name,
                "display_name": r.display_name,
                "description": r.description,
                "source": r.source,
                "is_system": r.is_system,
                "wildcards": wildcards,
                "expanded_permission_count": len(expanded),
            })

        return {"roles": result}


@register_tool
class ListEmployees(AssistantTool):
    name = "list_employees"
    description = "List all employees/users on this hub"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import LocalUser
        hub_id = request.session.get('hub_id')
        users = LocalUser.objects.filter(
            hub_id=hub_id, is_deleted=False, is_active=True
        ).order_by('name')

        return {
            "employees": [
                {
                    "id": str(u.id),
                    "name": u.name,
                    "email": u.email,
                    "role": u.get_role_name(),
                    "is_cloud_user": u.is_cloud_user,
                }
                for u in users
            ]
        }


@register_tool
class ListTaxClasses(AssistantTool):
    name = "list_tax_classes"
    description = "List all tax classes/rates configured on this hub"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import TaxClass
        tax_classes = TaxClass.objects.filter(is_active=True).order_by('order', 'rate')

        return {
            "tax_classes": [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "rate": str(tc.rate),
                    "description": tc.description,
                    "is_default": tc.is_default,
                }
                for tc in tax_classes
            ]
        }


@register_tool
class CreateTaxClass(AssistantTool):
    name = "create_tax_class"
    description = "Create a tax class (IVA rate) on this hub. Use for setting up tax rates like General 21%, Reducido 10%, etc."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tax class name (e.g. 'General', 'Reducido')"},
            "rate": {"type": "number", "description": "Tax rate percentage (e.g. 21, 10, 4, 0)"},
            "is_default": {"type": "boolean", "description": "Whether this is the default tax class"},
            "description": {"type": "string", "description": "Optional description"},
        },
        "required": ["name", "rate"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from decimal import Decimal
        from apps.configuration.models import TaxClass

        name = args['name']
        rate = Decimal(str(args['rate']))
        is_default = args.get('is_default', False)
        description = args.get('description', '')

        if TaxClass.objects.filter(name=name).exists():
            return {"error": f"Tax class '{name}' already exists"}

        if is_default:
            TaxClass.objects.filter(is_default=True).update(is_default=False)

        order = TaxClass.objects.count() + 1
        tc = TaxClass.objects.create(
            name=name,
            rate=rate,
            is_default=is_default,
            description=description,
            order=order,
        )
        return {"id": tc.id, "name": tc.name, "rate": str(tc.rate), "is_default": tc.is_default, "created": True}


@register_tool
class SetTaxConfig(AssistantTool):
    name = "set_tax_config"
    description = "Set the default tax rate and tax-included setting on the store configuration"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "tax_rate": {"type": "number", "description": "Default tax rate percentage (e.g. 21)"},
            "tax_included": {"type": "boolean", "description": "Whether prices include tax"},
        },
        "required": ["tax_rate", "tax_included"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from decimal import Decimal
        from apps.configuration.models import StoreConfig, TaxClass
        store = StoreConfig.get_solo()
        store.tax_rate = args['tax_rate']
        store.tax_included = args['tax_included']
        # Auto-link default_tax_class if a TaxClass matches the tax_rate
        rate = Decimal(str(args['tax_rate']))
        matching_tc = TaxClass.objects.filter(rate=rate).first()
        if matching_tc:
            store.default_tax_class = matching_tc
        store.save()
        result = {"tax_rate": str(store.tax_rate), "tax_included": store.tax_included}
        if store.default_tax_class:
            result["default_tax_class"] = store.default_tax_class.name
        return result


# ============================================================================
# WRITE TOOLS (require confirmation)
# ============================================================================

@register_tool
class UpdateStoreConfig(AssistantTool):
    name = "update_store_config"
    description = "Update store/business configuration: name, address, VAT number, phone, email, tax settings"
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "business_name": {"type": ["string", "null"], "description": "Business name"},
            "business_address": {"type": ["string", "null"], "description": "Business address"},
            "vat_number": {"type": ["string", "null"], "description": "VAT/Tax ID number"},
            "phone": {"type": ["string", "null"], "description": "Phone number"},
            "email": {"type": ["string", "null"], "description": "Email address"},
            "tax_rate": {"type": ["number", "null"], "description": "Default tax rate percentage"},
            "tax_included": {"type": ["boolean", "null"], "description": "Whether prices include tax"},
        },
        "required": ["business_name", "business_address", "vat_number", "phone", "email", "tax_rate", "tax_included"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        updated = []

        for field in ['business_name', 'business_address', 'vat_number', 'phone', 'email', 'tax_rate', 'tax_included']:
            value = args.get(field)
            if value is not None:
                setattr(store, field, value)
                updated.append(field)

        if updated:
            store.save()

        return {"success": True, "updated_fields": updated}


@register_tool
class InstallModules(AssistantTool):
    name = "install_modules"
    description = (
        "Install modules from the Cloud marketplace in bulk. "
        "Takes a list of module slugs (from get_module_catalog) and downloads+installs "
        "them all at once, then schedules a server restart. "
        "Use this when the user asks to install modules for their business. "
        "Always call get_module_catalog first to get valid module_id slugs. "
        "Install all needed modules in a SINGLE call to avoid multiple restarts."
    )
    short_description = (
        "Download and install modules from the marketplace in bulk (triggers server restart). "
        "Use get_module_catalog first. Install ALL needed modules in ONE call."
    )
    module_id = None
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "module_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of module_id slugs to install (e.g., ['customers', 'inventory', 'sales'])",
            },
        },
        "required": ["module_ids"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import requests as http_requests
        from pathlib import Path
        from django.conf import settings
        from django.core.cache import cache

        module_ids = args.get('module_ids', [])
        if not module_ids:
            return {"error": "module_ids list is required"}

        modules_dir = Path(settings.MODULES_DIR)
        installed_ids = set()
        if modules_dir.exists():
            for d in modules_dir.iterdir():
                if d.is_dir() and not d.name.startswith('.'):
                    installed_ids.add(d.name.lstrip('_'))

        # Always ensure the assistant module is included — it's required
        # for the AI to continue operating after restart.
        if 'assistant' not in module_ids and 'assistant' not in installed_ids:
            module_ids = list(module_ids) + ['assistant']

        to_install = [mid for mid in module_ids if mid not in installed_ids]
        already = [mid for mid in module_ids if mid in installed_ids]

        if not to_install:
            return {
                "message": "All requested modules are already installed",
                "already_installed": already,
            }

        base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com')
        from apps.configuration.models import HubConfig
        hub_config = HubConfig.get_solo()
        auth_token = hub_config.hub_jwt or hub_config.cloud_api_token

        modules_to_install = []
        for mid in to_install:
            modules_to_install.append({
                'slug': mid,
                'name': mid,
                'download_url': f"{base_url}/api/marketplace/modules/{mid}/download/",
            })

        try:
            from apps.core.services.module_install_service import ModuleInstallService

            # Resolve transitive dependencies before installing
            try:
                dep_modules = ModuleInstallService._resolve_dependencies(
                    set(to_install), installed_ids, base_url, auth_token,
                )
                if dep_modules:
                    dep_names = [m['slug'] for m in dep_modules]
                    logger.info("[ASSISTANT] Auto-resolved dependencies: %s", dep_names)
                    modules_to_install.extend(dep_modules)
                    to_install.extend(dep_names)
            except Exception as e:
                logger.warning("[ASSISTANT] Dependency resolution failed, continuing: %s", e)

            result = ModuleInstallService.bulk_download_and_install(
                modules_to_install, auth_token,
            )

            if result.installed > 0:
                cache.delete('marketplace:modules_list')
                cache.delete('marketplace:installed_modules')

                ModuleInstallService.run_post_install(
                    load_all=True, run_migrations=True, schedule_restart=False,
                )

                # Flag that a restart is needed AFTER the agentic loop
                # finishes and sends the result to the frontend.
                # The _background_task in views.py checks this flag.
                cache.set('assistant_restart_pending', True, timeout=300)

                try:
                    from apps.marketplace.views import _create_roles_for_installed_modules
                    _create_roles_for_installed_modules(to_install)
                except Exception:
                    pass

                # Register installed modules with Cloud so ensure_modules
                # can restore them after container restart
                ModuleInstallService.notify_cloud_installations(
                    to_install, auth_token,
                )

            return {
                "message": (
                    f"Installed {result.installed} modules. Server restart scheduled. "
                    "IMPORTANT: The server will restart in a few seconds to load the new modules. "
                    "Do NOT call any tools from the newly installed modules yet — they are not available until after the restart. "
                    "Tell the user that the modules were installed and the system will restart shortly. "
                    "The user should send a new message after the restart to continue setup."
                ),
                "installed_count": result.installed,
                "already_installed": already,
                "errors": result.errors if result.errors else [],
                "requires_restart": True,
            }
        except Exception as e:
            logger.error(f"[ASSISTANT] Module install error: {e}", exc_info=True)
            return {"error": f"Failed to install modules: {str(e)}"}


@register_tool
class LoadModuleTools(AssistantTool):
    name = "load_module_tools"
    description = (
        "Load AI tools for specific modules. Only hub core tools are available by default. "
        "Call this before using any module-specific tool. "
        "Dependencies are resolved automatically — e.g. loading 'sales' also loads 'customers' and 'inventory'. "
        "Loaded tools persist across messages in this conversation. "
        "Example: load_module_tools(modules=['inventory']) to create products."
    )
    short_description = "Load tools for a module (dependencies auto-included). Call before using module-specific tools."
    parameters = {
        "type": "object",
        "properties": {
            "modules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Module IDs to load (e.g. ['inventory'] or ['sales']). Dependencies auto-included.",
            },
        },
        "required": ["modules"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from assistant.tools import TOOL_REGISTRY, _get_active_module_ids, resolve_module_dependencies

        requested = args.get('modules', [])
        if not requested:
            return {"error": "Provide at least one module ID"}

        active = set(_get_active_module_ids())

        # Resolve dependencies automatically
        resolved, dep_map = resolve_module_dependencies(requested, active)

        not_found = [mid for mid in resolved if mid not in active]
        to_load = [mid for mid in resolved if mid in active]

        loaded_names = []
        for mid in to_load:
            for tool in TOOL_REGISTRY.values():
                if tool.module_id == mid:
                    loaded_names.append(tool.name)

        result = {
            "loaded_tools": loaded_names,
            "loaded_count": len(loaded_names),
            "loaded_for": to_load,
        }
        if dep_map:
            result["auto_included_deps"] = dep_map
        if not_found:
            result["not_found"] = not_found
        return result


@register_tool
class UnloadModuleTools(AssistantTool):
    name = "unload_module_tools"
    description = (
        "Unload AI tools for modules you no longer need to free up context. "
        "Call this when switching to a different domain (e.g. after creating products, "
        "unload inventory before loading tables). "
        "Hub core tools are never unloaded."
    )
    short_description = "Unload module tools to free context. Use when switching domains."
    parameters = {
        "type": "object",
        "properties": {
            "modules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Module IDs to unload (e.g. ['inventory', 'customers']).",
            },
        },
        "required": ["modules"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        modules = args.get('modules', [])
        if not modules:
            return {"error": "Provide at least one module ID"}
        return {
            "unloaded": modules,
            "message": f"Unloaded tools for: {', '.join(modules)}.",
        }
