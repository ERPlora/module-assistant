"""
P0 Hub Core Tools — always available.

These tools operate on the Hub's core configuration: HubConfig, StoreConfig,
TaxClass, modules, roles, and employees.
"""
from assistant.tools import AssistantTool, register_tool


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
class ListAvailableBlocks(AssistantTool):
    name = "list_available_blocks"
    description = "List all available functional blocks from Cloud marketplace. Returns block names, descriptions, and categories."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import requests as http_requests
        from django.conf import settings

        base_url = getattr(settings, 'CLOUD_API_URL', 'https://erplora.com')
        try:
            resp = http_requests.get(
                f"{base_url}/api/marketplace/solutions/",
                timeout=10,
            )
            if resp.status_code == 200:
                blocks = resp.json()
                # Return simplified list
                return {
                    "blocks": [
                        {
                            "slug": b.get("slug", ""),
                            "name": b.get("name", ""),
                            "tagline": b.get("tagline", ""),
                            "block_type": b.get("block_type", ""),
                            "icon": b.get("icon", ""),
                        }
                        for b in (blocks if isinstance(blocks, list) else blocks.get("results", []))
                    ]
                }
            return {"error": f"Cloud API returned {resp.status_code}"}
        except Exception as e:
            return {"error": f"Failed to fetch blocks: {str(e)}"}


@register_tool
class GetSelectedBlocks(AssistantTool):
    name = "get_selected_blocks"
    description = "Get the functional blocks currently selected for this hub"
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
            "selected_blocks": config.selected_blocks or [],
            "solution_slug": config.solution_slug,
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
            # Try to get description and navigation from module.py
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

        # Get installed module IDs
        modules_dir = Path(settings.MODULES_DIR)
        installed_ids = set()
        if modules_dir.exists():
            for d in modules_dir.iterdir():
                if d.is_dir() and not d.name.startswith('.'):
                    installed_ids.add(d.name.lstrip('_'))

        # Fetch Hub token for authenticated requests (is_owned field)
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
                    "functions": m.get('functions_names', []),
                    "industries": m.get('industries', []),
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


# ============================================================================
# WRITE TOOLS (require confirmation)
# ============================================================================

@register_tool
class UpdateStoreConfig(AssistantTool):
    name = "update_store_config"
    description = "Update store/business configuration: name, address, VAT number, phone, email, tax settings"
    requires_confirmation = True
    required_permission = "assistant.use_chat"
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
class SelectBlocks(AssistantTool):
    name = "select_blocks"
    description = "Select functional blocks for this hub. This determines which modules and roles are available."
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "block_slugs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of block slugs to select (e.g., ['pos_retail', 'inventory', 'crm'])",
            },
        },
        "required": ["block_slugs"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig
        config = HubConfig.get_solo()
        block_slugs = args.get('block_slugs', [])

        config.selected_blocks = block_slugs
        if block_slugs:
            config.solution_slug = block_slugs[0]
        config.save()

        return {
            "success": True,
            "selected_blocks": block_slugs,
            "count": len(block_slugs),
        }


@register_tool
class EnableModule(AssistantTool):
    name = "enable_module"
    description = "Enable a module that is currently disabled (removes the _ prefix from its directory name)"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "Module ID to enable"},
        },
        "required": ["module_id"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import os
        from django.conf import settings as django_settings
        module_id = args['module_id']
        modules_dir = django_settings.MODULES_DIR

        disabled_path = os.path.join(modules_dir, f"_{module_id}")
        enabled_path = os.path.join(modules_dir, module_id)

        if os.path.exists(enabled_path):
            return {"success": True, "message": f"Module {module_id} is already enabled"}

        if os.path.exists(disabled_path):
            os.rename(disabled_path, enabled_path)
            return {"success": True, "message": f"Module {module_id} enabled. Restart required."}

        return {"success": False, "error": f"Module {module_id} not found"}


@register_tool
class DisableModule(AssistantTool):
    name = "disable_module"
    description = "Disable a module (adds _ prefix to its directory name)"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "Module ID to disable"},
        },
        "required": ["module_id"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        import os
        from django.conf import settings as django_settings
        module_id = args['module_id']
        modules_dir = django_settings.MODULES_DIR

        if module_id == 'assistant':
            return {"success": False, "error": "Cannot disable the assistant module"}

        enabled_path = os.path.join(modules_dir, module_id)
        disabled_path = os.path.join(modules_dir, f"_{module_id}")

        if os.path.exists(disabled_path):
            return {"success": True, "message": f"Module {module_id} is already disabled"}

        if os.path.exists(enabled_path):
            os.rename(enabled_path, disabled_path)
            return {"success": True, "message": f"Module {module_id} disabled. Restart required."}

        return {"success": False, "error": f"Module {module_id} not found"}


@register_tool
class CreateRole(AssistantTool):
    name = "create_role"
    description = "Create a custom role with specific permission wildcards"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Role name (e.g., 'cashier')"},
            "display_name": {"type": "string", "description": "Display name (e.g., 'Cashier')"},
            "description": {"type": "string", "description": "Role description"},
            "wildcards": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Permission wildcards (e.g., ['sales.*', 'inventory.view_*'])",
            },
        },
        "required": ["name", "display_name", "description", "wildcards"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import Role, RolePermission
        hub_id = request.session.get('hub_id')

        role = Role.objects.create(
            hub_id=hub_id,
            name=args['name'],
            display_name=args['display_name'],
            description=args.get('description', ''),
            source='custom',
            is_system=False,
        )

        for wildcard in args.get('wildcards', []):
            RolePermission.objects.create(
                hub_id=hub_id,
                role=role,
                wildcard=wildcard,
            )

        return {
            "success": True,
            "role_id": str(role.id),
            "name": role.name,
            "wildcards": args.get('wildcards', []),
        }


@register_tool
class CreateEmployee(AssistantTool):
    name = "create_employee"
    description = "Create a new local employee with name, email, role, and PIN"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Employee full name"},
            "email": {"type": "string", "description": "Employee email"},
            "pin": {"type": "string", "description": "4-digit PIN code"},
            "role_name": {"type": "string", "description": "Role name to assign (e.g., 'admin', 'manager', 'employee')"},
        },
        "required": ["name", "email", "pin", "role_name"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import LocalUser, Role
        hub_id = request.session.get('hub_id')

        # Find role
        role_obj = Role.objects.filter(
            hub_id=hub_id, name=args['role_name'], is_deleted=False
        ).first()

        user = LocalUser(
            hub_id=hub_id,
            name=args['name'],
            email=args['email'],
            role=args.get('role_name', 'employee'),
            role_obj=role_obj,
        )
        user.set_pin(args['pin'])
        user.save()

        return {
            "success": True,
            "employee_id": str(user.id),
            "name": user.name,
            "role": user.get_role_name(),
        }


@register_tool
class CreateTaxClass(AssistantTool):
    name = "create_tax_class"
    description = "Create a new tax class/rate (e.g., 'IVA General 21%', 'IGIC 7%')"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tax class name (e.g., 'IVA General 21%')"},
            "rate": {"type": "number", "description": "Tax rate as percentage (e.g., 21.0)"},
            "description": {"type": "string", "description": "Optional description"},
            "is_default": {"type": "boolean", "description": "Whether this is the default tax class"},
        },
        "required": ["name", "rate", "description", "is_default"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import TaxClass

        if args.get('is_default'):
            TaxClass.objects.filter(is_default=True).update(is_default=False)

        tc = TaxClass.objects.create(
            name=args['name'],
            rate=args['rate'],
            description=args.get('description', ''),
            is_default=args.get('is_default', False),
        )

        return {
            "success": True,
            "tax_class_id": tc.id,
            "name": tc.name,
            "rate": str(tc.rate),
        }
