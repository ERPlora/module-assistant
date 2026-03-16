"""
Simplified setup tools with strict schemas.

These tools consolidate multiple setup steps into single calls,
reducing the number of tool invocations needed during hub configuration.
All tools use strict=True for reliable structured output.
"""
import logging

from django.utils.text import slugify

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class SetupBusiness(AssistantTool):
    """Configure region, business info, tax, and blueprint in one call."""

    name = "setup_business"
    description = (
        "Set up the entire business configuration in one step: "
        "regional settings (language, timezone, country, currency), "
        "business info (name, address, VAT), tax config (rate, included), "
        "and optionally install a blueprint (business type). "
        "Call this once during initial setup instead of multiple separate tools."
    )
    requires_confirmation = True
    required_permission = None
    setup_only = True
    strict = True
    parameters = {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "Language code (e.g., 'es', 'en', 'de', 'fr')",
            },
            "timezone": {
                "type": "string",
                "description": "Timezone (e.g., 'Europe/Madrid', 'America/New_York')",
            },
            "country_code": {
                "type": "string",
                "description": "ISO 3166-1 alpha-2 country code (e.g., 'ES', 'US')",
            },
            "currency": {
                "type": "string",
                "description": "ISO 4217 currency code (e.g., 'EUR', 'USD')",
            },
            "business_name": {
                "type": "string",
                "description": "Business name",
            },
            "business_address": {
                "type": "string",
                "description": "Full business address",
            },
            "vat_number": {
                "type": "string",
                "description": "VAT/Tax ID number (e.g., CIF, NIF, EIN). Use empty string if not provided.",
            },
            "tax_rate": {
                "type": "number",
                "description": "Default tax rate percentage (e.g., 21.0 for Spain)",
            },
            "tax_included": {
                "type": "boolean",
                "description": "Whether prices include tax by default",
            },
            "business_type_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Blueprint business type codes to install (e.g., ['restaurant']). Empty array to skip blueprint.",
            },
        },
        "required": [
            "language", "timezone", "country_code", "currency",
            "business_name", "business_address", "vat_number",
            "tax_rate", "tax_included", "business_type_codes",
        ],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig, StoreConfig

        # 1. Regional config
        hub_config = HubConfig.get_solo()
        hub_config.language = args['language']
        hub_config.timezone = args['timezone']
        hub_config.country_code = args['country_code']
        hub_config.currency = args['currency']
        hub_config.save()

        # 2. Business info + tax
        store_config = StoreConfig.get_solo()
        store_config.business_name = args['business_name']
        store_config.business_address = args['business_address']
        store_config.vat_number = args['vat_number']
        store_config.tax_rate = args['tax_rate']
        store_config.tax_included = args['tax_included']
        store_config.is_configured = True
        store_config.save()

        result = {
            "success": True,
            "regional": {
                "language": hub_config.language,
                "timezone": hub_config.timezone,
                "country_code": hub_config.country_code,
                "currency": hub_config.currency,
            },
            "business": {
                "name": store_config.business_name,
                "tax_rate": str(store_config.tax_rate),
                "tax_included": store_config.tax_included,
            },
        }

        # 3. Blueprint (optional)
        type_codes = args.get('business_type_codes', [])
        if type_codes:
            try:
                from apps.core.services.blueprint_service import BlueprintService
                bp_result = BlueprintService.install_blueprint(hub_config, type_codes)
                result["blueprint"] = {
                    "installed": True,
                    "modules": bp_result.get('modules_installed', []),
                    "roles": bp_result.get('roles_created', []),
                }
            except Exception as e:
                logger.exception("Blueprint install failed in setup_business")
                result["blueprint"] = {
                    "installed": False,
                    "error": str(e),
                }

        # 4. Mark setup complete
        hub_config.is_configured = True
        hub_config.save()

        return result


@register_tool
class CreateEmployee(AssistantTool):
    """Create a single employee."""

    name = "create_employee"
    description = (
        "Create a single employee with name, role, PIN, and optional email. "
        "For creating multiple employees at once, use bulk_create_employees instead."
    )
    requires_confirmation = True
    required_permission = 'accounts.add_localuser'
    strict = False  # LLM sends name under various keys
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Full name of the employee. Required."},
            "role_name": {"type": "string", "description": "Role name (e.g., 'admin', 'manager', 'employee'). Default 'employee'."},
            "pin": {"type": "string", "description": "4-digit PIN code for login."},
            "email": {"type": "string", "description": "Email address. Optional."},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import LocalUser, Role
        import uuid as _uuid

        hub_id = request.session.get('hub_id')

        # Accept various param shapes the LLM might use
        name = (
            args.get('name') or args.get('full_name')
            or args.get('employee_name') or args.get('nombre')
            or args.get('display_name') or ''
        )
        if not name:
            first = args.get('first_name', '')
            last = args.get('last_name', '')
            name = f"{first} {last}".strip()
        if not name:
            return {"error": "name is required for create_employee"}

        # Check for existing
        existing = LocalUser.objects.filter(hub_id=hub_id, name=name).first()
        if existing:
            return {"message": f"Employee '{name}' already exists", "employee_id": str(existing.id)}

        role_name = args.get('role_name') or args.get('role') or 'employee'
        role_obj = Role.objects.filter(
            hub_id=hub_id, name__iexact=role_name, is_deleted=False,
        ).first()
        if not role_obj:
            role_obj = Role.objects.filter(
                hub_id=hub_id, display_name__iexact=role_name, is_deleted=False,
            ).first()

        # Determine legacy role field
        legacy_role = 'employee'
        role_lower = role_name.lower()
        if role_lower in ('admin', 'administrator', 'directora', 'director', 'gerente'):
            legacy_role = 'admin'
        elif role_lower in ('manager', 'encargado', 'encargada', 'jefe de sala', 'jefa de sala'):
            legacy_role = 'manager'
        elif role_lower in ('viewer', 'visor', 'observador'):
            legacy_role = 'viewer'

        email = args.get('email', '')
        if not email:
            email = f"{_uuid.uuid4().hex[:8]}@placeholder.local"

        user = LocalUser(
            hub_id=hub_id,
            name=name,
            email=email,
            role=legacy_role,
            role_obj=role_obj,
            is_active=True,
        )
        pin = args.get('pin') or args.get('pin_code')
        if pin:
            user.set_pin(str(pin))
        user.save()
        return {"employee_id": str(user.id), "name": user.name, "created": True}


@register_tool
class CreateRole(AssistantTool):
    """Create a custom role with permission wildcards."""

    name = "create_role"
    description = (
        "Create a custom role with permission wildcards. "
        "Use this when the business needs roles beyond the default ones (admin, manager, employee, viewer)."
    )
    requires_confirmation = True
    required_permission = 'accounts.add_role'
    strict = False  # LLM sends name under various keys
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Role name (e.g., 'chef', 'recepcionista'). Required."},
            "display_name": {"type": "string", "description": "Display name. Defaults to name."},
            "description": {"type": "string", "description": "Optional description."},
            "wildcards": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Permission wildcards (e.g., ['sales.*', 'inventory.view_*']).",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import Role, RolePermission
        hub_id = request.session.get('hub_id')

        name = (
            args.get('name') or args.get('role_name')
            or args.get('title') or args.get('label')
        )
        if not name:
            return {"error": "Missing 'name' for create_role"}

        existing = Role.objects.filter(
            hub_id=hub_id, name=name, is_deleted=False,
        ).first()
        if existing:
            return {"message": f"Role '{name}' already exists", "role_id": str(existing.id)}

        wildcards = (
            args.get('wildcards') or args.get('permissions')
            or args.get('permission_wildcards') or []
        )

        role = Role.objects.create(
            hub_id=hub_id,
            name=name,
            display_name=args.get('display_name', name),
            description=args.get('description', ''),
            source='custom',
            is_system=False,
        )
        for wildcard in wildcards:
            RolePermission.objects.create(
                hub_id=hub_id,
                role=role,
                wildcard=wildcard,
            )
        return {"role_id": str(role.id), "name": role.name, "created": True}


@register_tool
class BulkCreateEmployees(AssistantTool):
    """Create multiple employees in one call."""

    name = "bulk_create_employees"
    description = (
        "Create multiple employees at once. Each employee gets a name, role, "
        "PIN code, and optional email. Use this instead of calling create_employee "
        "multiple times."
    )
    requires_confirmation = True
    required_permission = 'accounts.add_localuser'
    strict = True
    parameters = {
        "type": "object",
        "properties": {
            "employees": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "first_name": {"type": "string", "description": "First name"},
                        "last_name": {"type": "string", "description": "Last name"},
                        "role": {
                            "type": "string",
                            "description": "Role name (e.g., 'admin', 'manager', 'employee', 'viewer')",
                        },
                        "pin": {
                            "type": "string",
                            "description": "4-digit PIN code for login",
                        },
                        "email": {
                            "type": "string",
                            "description": "Email address. Use empty string if not provided.",
                        },
                    },
                    "required": ["first_name", "last_name", "role", "pin", "email"],
                    "additionalProperties": False,
                },
                "description": "List of employees to create",
            },
        },
        "required": ["employees"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.accounts.models import LocalUser, Role
        import uuid as _uuid

        hub_id = request.session.get('hub_id')
        created = []
        errors = []

        for emp in args['employees']:
            name = f"{emp['first_name']} {emp['last_name']}".strip()
            try:
                # Check for existing employee by name
                existing = LocalUser.objects.filter(hub_id=hub_id, name=name).first()
                if existing:
                    created.append({
                        "id": str(existing.pk),
                        "name": existing.name,
                        "status": "already_exists",
                    })
                    continue

                # Find role object
                role_name = emp['role']
                role_obj = Role.objects.filter(
                    hub_id=hub_id, name__iexact=role_name, is_deleted=False,
                ).first()
                if not role_obj:
                    # Try display_name match
                    role_obj = Role.objects.filter(
                        hub_id=hub_id, display_name__iexact=role_name, is_deleted=False,
                    ).first()

                # Determine legacy role field value
                legacy_role = 'employee'
                role_lower = role_name.lower()
                if role_lower in ('admin', 'administrator', 'directora', 'director'):
                    legacy_role = 'admin'
                elif role_lower in ('manager', 'gerente', 'encargado', 'encargada'):
                    legacy_role = 'manager'
                elif role_lower in ('viewer', 'visor', 'observador'):
                    legacy_role = 'viewer'

                email = emp.get('email') or ''
                if not email:
                    email = f"{_uuid.uuid4().hex[:8]}@placeholder.local"

                user = LocalUser(
                    hub_id=hub_id,
                    name=name,
                    email=email,
                    role=legacy_role,
                    role_obj=role_obj,
                    is_active=True,
                )
                user.save()
                pin = emp.get('pin')
                if pin:
                    user.set_pin(str(pin))

                created.append({
                    "id": str(user.pk),
                    "name": user.name,
                    "role": role_obj.name if role_obj else legacy_role,
                    "status": "created",
                })
            except Exception as e:
                errors.append(f"{name}: {str(e)}")

        return {
            "success": len(errors) == 0,
            "created_count": len([c for c in created if c.get('status') == 'created']),
            "created": created,
            "errors": errors,
        }


@register_tool
class BulkCreateServices(AssistantTool):
    """Create service categories and services in bulk."""

    name = "bulk_create_services"
    description = (
        "Create service categories with their services in one call. "
        "Each category contains a list of services with name, duration, and price. "
        "Requires the 'services' module to be installed."
    )
    requires_confirmation = True
    required_permission = 'services.add_service'
    strict = True
    parameters = {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Category name"},
                        "services": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Service name"},
                                    "duration_minutes": {
                                        "type": "integer",
                                        "description": "Duration in minutes",
                                    },
                                    "price": {
                                        "type": "string",
                                        "description": "Price as decimal string (e.g., '25.00')",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Service description. Use empty string if none.",
                                    },
                                },
                                "required": ["name", "duration_minutes", "price", "description"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["name", "services"],
                    "additionalProperties": False,
                },
                "description": "List of categories, each with their services",
            },
        },
        "required": ["categories"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from decimal import Decimal, InvalidOperation

        try:
            from services.models import ServiceCategory, Service
        except ImportError:
            return {
                "success": False,
                "error": "The 'services' module is not installed.",
            }

        created_categories = []
        created_services = []
        errors = []

        for cat_data in args['categories']:
            try:
                category, _ = ServiceCategory.objects.get_or_create(
                    name=cat_data['name'],
                    defaults={'slug': slugify(cat_data['name'])},
                )

                for svc_data in cat_data['services']:
                    try:
                        price = Decimal(svc_data['price'])
                    except (InvalidOperation, ValueError):
                        errors.append({
                            "service": svc_data['name'],
                            "error": f"Invalid price: {svc_data['price']}",
                        })
                        continue

                    service, was_created = Service.objects.get_or_create(
                        name=svc_data['name'],
                        category=category,
                        defaults={
                            'slug': slugify(svc_data['name']),
                            'duration_minutes': svc_data['duration_minutes'],
                            'price': price,
                            'description': svc_data.get('description', ''),
                        },
                    )
                    if was_created:
                        created_services.append({
                            "name": service.name,
                            "category": category.name,
                            "price": str(service.price),
                            "duration": service.duration_minutes,
                        })

                created_categories.append(category.name)
            except Exception as e:
                errors.append({
                    "category": cat_data['name'],
                    "error": str(e),
                })

        return {
            "success": len(errors) == 0,
            "categories_count": len(created_categories),
            "services_count": len(created_services),
            "categories": created_categories,
            "services": created_services,
            "errors": errors,
        }


@register_tool
class CompleteSetup(AssistantTool):
    """Mark hub setup as complete."""

    name = "complete_setup"
    description = "Mark the hub setup as complete. Call this after all configuration steps are done."
    requires_confirmation = True
    required_permission = None
    setup_only = True
    strict = True
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig, StoreConfig

        hub_config = HubConfig.get_solo()
        store_config = StoreConfig.get_solo()

        hub_config.is_configured = True
        hub_config.save()
        store_config.is_configured = True
        store_config.save()

        return {
            "success": True,
            "message": "Setup completed. Hub is now fully configured.",
        }
