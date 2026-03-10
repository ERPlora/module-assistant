"""
ExecutePlan — multi-step configuration tool.

Executes a plan (or subset) atomically with a single user confirmation.
Supports: regional config, business info, tax, modules, roles, employees,
categories, products, services, payment methods, business hours, zones,
tables, and blueprint installation.
"""
import logging
import os

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class ExecutePlan(AssistantTool):
    name = "execute_plan"
    description = (
        "Execute a business configuration plan (or subset) atomically. "
        "Takes a list of steps, each with an 'action' and 'params'. "
        "Supported actions: set_regional_config, set_business_info, set_tax_config, "
        "enable_module, disable_module, create_role, create_employee, "
        "create_tax_class, update_store_config, complete_setup, "
        "create_category, create_product, create_service_category, create_service, "
        "create_payment_method, set_business_hours, create_zone, create_table, "
        "bulk_create_zones, bulk_create_tables, bulk_set_business_hours, install_blueprint. "
        "IMPORTANT: create_product accepts 'categories' (list of category names) to assign the product to categories. "
        "Always include 'categories' when creating products so they are properly categorized. "
        "Create categories first (create_category), then reference them by name in create_product. "
        "All steps are executed in order. If any step fails, the error is reported "
        "but remaining steps continue. "
        "Use this after presenting a plan to the user, or for partial execution "
        "(e.g., just installing modules or just creating roles). "
        "CRITICAL: When the user confirms a plan you presented, the steps in execute_plan "
        "MUST match EXACTLY what you described — same names, same prices, same quantities. "
        "Never substitute generic or simplified data for the specific details you showed the user."
    )
    requires_confirmation = True
    required_permission = None
    examples = [
        {"steps": [
            {"action": "set_regional_config", "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"}},
            {"action": "set_business_info", "params": {"business_name": "Salón María", "business_address": "C/ Gran Vía 10, Madrid"}},
            {"action": "set_tax_config", "params": {"tax_rate": 21.0, "tax_included": True}},
            {"action": "create_category", "params": {"name": "Bebidas"}},
            {"action": "create_product", "params": {"name": "Agua mineral", "price": 2.00, "stock": 50, "categories": ["Bebidas"]}},
            {"action": "create_service_category", "params": {"name": "Cortes"}},
            {"action": "create_service", "params": {"name": "Corte + Peinado", "price": 25, "duration_minutes": 45, "category": "Cortes"}},
        ]},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "List of steps to execute",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Action name (e.g., 'set_regional_config', 'enable_module', 'create_role')",
                        },
                        "params": {
                            "type": "object",
                            "description": "Parameters for the action",
                        },
                    },
                    "required": ["action", "params"],
                },
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        steps = args.get('steps', [])
        if not steps:
            return {"success": False, "error": "No steps provided"}

        results = []
        errors = []

        for i, step in enumerate(steps):
            action = step.get('action', '')
            params = step.get('params', {})

            try:
                result = self._execute_step(action, params, request)
                results.append({
                    'step': i + 1,
                    'action': action,
                    'success': True,
                    'result': result,
                })
            except Exception as e:
                logger.error(f"[ASSISTANT] Plan step {i+1} ({action}) failed: {e}", exc_info=True)
                error_msg = str(e)
                results.append({
                    'step': i + 1,
                    'action': action,
                    'success': False,
                    'error': error_msg,
                })
                errors.append(f"Step {i+1} ({action}): {error_msg}")

        success_count = sum(1 for r in results if r['success'])

        return {
            "success": len(errors) == 0,
            "total_steps": len(steps),
            "succeeded": success_count,
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }

    def _execute_step(self, action, params, request):
        """Execute a single plan step."""
        dispatch = {
            'set_regional_config': self._set_regional_config,
            'set_business_info': self._set_business_info,
            'set_tax_config': self._set_tax_config,
            'enable_module': self._enable_module,
            'disable_module': self._disable_module,
            'create_role': lambda p: self._create_role(p, request),
            'create_employee': lambda p: self._create_employee(p, request),
            'create_tax_class': self._create_tax_class,
            'update_store_config': self._update_store_config,
            'complete_setup': lambda p: self._complete_setup(),
            'create_category': self._create_category,
            'create_product': self._create_product,
            'create_service_category': self._create_service_category,
            'create_service': self._create_service,
            'create_payment_method': self._create_payment_method,
            'set_business_hours': self._set_business_hours,
            'create_zone': self._create_zone,
            'create_table': self._create_table,
            'bulk_create_zones': self._bulk_create_zones,
            'bulk_create_tables': self._bulk_create_tables,
            'bulk_set_business_hours': self._set_business_hours,
            'install_blueprint': self._install_blueprint,
        }

        handler = dispatch.get(action)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")
        return handler(params)

    # ── Hub Configuration ──────────────────────────────────────────

    def _set_regional_config(self, params):
        from apps.configuration.models import HubConfig
        config = HubConfig.get_solo()
        updated = []
        for field in ['language', 'timezone', 'country_code', 'currency']:
            value = params.get(field)
            if value is not None:
                setattr(config, field, value)
                updated.append(field)
        if updated:
            config.save()
        return {"updated_fields": updated}

    def _set_business_info(self, params):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        for field in ['business_name', 'business_address', 'vat_number', 'phone', 'email']:
            value = params.get(field)
            if value is not None:
                setattr(store, field, value)
        store.save()
        return {"business_name": store.business_name}

    def _set_tax_config(self, params):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        if 'tax_rate' in params:
            store.tax_rate = params['tax_rate']
        if 'tax_included' in params:
            store.tax_included = params['tax_included']
        store.is_configured = True
        store.save()
        return {"tax_rate": str(store.tax_rate), "tax_included": store.tax_included}

    def _enable_module(self, params):
        from django.conf import settings as django_settings
        module_id = params.get('module_id', '')
        if not module_id:
            raise ValueError("module_id is required")

        modules_dir = django_settings.MODULES_DIR
        disabled_path = os.path.join(str(modules_dir), f"_{module_id}")
        enabled_path = os.path.join(str(modules_dir), module_id)

        if os.path.exists(enabled_path):
            return {"message": f"Module {module_id} is already enabled"}
        if os.path.exists(disabled_path):
            os.rename(disabled_path, enabled_path)
            return {"message": f"Module {module_id} enabled"}
        return {"message": f"Module {module_id} not found (may need to be installed)"}

    def _disable_module(self, params):
        from django.conf import settings as django_settings
        module_id = params.get('module_id', '')
        if not module_id or module_id == 'assistant':
            raise ValueError("Cannot disable the assistant module")

        modules_dir = django_settings.MODULES_DIR
        enabled_path = os.path.join(str(modules_dir), module_id)
        disabled_path = os.path.join(str(modules_dir), f"_{module_id}")

        if os.path.exists(disabled_path):
            return {"message": f"Module {module_id} is already disabled"}
        if os.path.exists(enabled_path):
            os.rename(enabled_path, disabled_path)
            return {"message": f"Module {module_id} disabled"}
        return {"message": f"Module {module_id} not found"}

    # ── Roles & Employees ──────────────────────────────────────────

    def _create_role(self, params, request):
        from apps.accounts.models import Role, RolePermission
        hub_id = request.session.get('hub_id')

        existing = Role.objects.filter(
            hub_id=hub_id, name=params['name'], is_deleted=False,
        ).first()
        if existing:
            return {"message": f"Role '{params['name']}' already exists", "role_id": str(existing.id)}

        role = Role.objects.create(
            hub_id=hub_id,
            name=params['name'],
            display_name=params.get('display_name', params['name']),
            description=params.get('description', ''),
            source='custom',
            is_system=False,
        )
        for wildcard in params.get('wildcards', []):
            RolePermission.objects.create(
                hub_id=hub_id,
                role=role,
                wildcard=wildcard,
            )
        return {"role_id": str(role.id), "name": role.name}

    def _create_employee(self, params, request):
        from apps.accounts.models import LocalUser, Role
        hub_id = request.session.get('hub_id')

        role_obj = Role.objects.filter(
            hub_id=hub_id, name=params.get('role_name', 'employee'), is_deleted=False,
        ).first()

        user = LocalUser(
            hub_id=hub_id,
            name=params['name'],
            email=params.get('email', ''),
            role=params.get('role_name', 'employee'),
            role_obj=role_obj,
        )
        if params.get('pin'):
            user.set_pin(params['pin'])
        user.save()
        return {"employee_id": str(user.id), "name": user.name}

    # ── Tax ────────────────────────────────────────────────────────

    def _create_tax_class(self, params):
        from apps.configuration.models import TaxClass

        if params.get('is_default'):
            TaxClass.objects.filter(is_default=True).update(is_default=False)

        tc = TaxClass.objects.create(
            name=params['name'],
            rate=params['rate'],
            description=params.get('description', ''),
            is_default=params.get('is_default', False),
        )
        return {"tax_class_id": tc.id, "name": tc.name, "rate": str(tc.rate)}

    # ── Store Config ───────────────────────────────────────────────

    def _update_store_config(self, params):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        updated = []
        for field in ['business_name', 'business_address', 'vat_number', 'phone', 'email', 'tax_rate', 'tax_included']:
            value = params.get(field)
            if value is not None:
                setattr(store, field, value)
                updated.append(field)
        if updated:
            store.save()
        return {"updated_fields": updated}

    def _complete_setup(self):
        from apps.configuration.models import HubConfig, StoreConfig
        hub_config = HubConfig.get_solo()
        store_config = StoreConfig.get_solo()
        hub_config.is_configured = True
        hub_config.save()
        store_config.is_configured = True
        store_config.save()
        return {"message": "Setup completed"}

    def _install_blueprint(self, params):
        """Install modules from blueprint for given business type codes."""
        from apps.configuration.models import HubConfig
        from apps.core.services.blueprint_service import BlueprintService

        hub_config = HubConfig.get_solo()
        type_codes = params.get('type_codes', [])
        sector = params.get('sector', '')

        if not type_codes:
            raise ValueError("type_codes is required (list of business type codes)")

        hub_config.selected_business_types = type_codes
        if sector:
            hub_config.business_sector = sector
        hub_config.save(update_fields=['selected_business_types', 'business_sector'])

        result = BlueprintService.install_blueprint(
            hub_config, type_codes, include_recommended=True,
        )

        return {
            "message": f"Blueprint installed for {type_codes}",
            "modules_installed": result.get('modules_installed', 0),
            "roles_created": result.get('roles_created', 0),
            "result": result,
        }

    # ── Inventory: Categories & Products ───────────────────────────

    def _create_category(self, params):
        from inventory.models import Category
        name = params.get('name', '')
        if not name:
            raise ValueError("Category name is required")

        existing = Category.objects.filter(name__iexact=name).first()
        if existing:
            return {"message": f"Category '{name}' already exists", "id": str(existing.id)}

        cat = Category.objects.create(
            name=name,
            description=params.get('description', ''),
            icon=params.get('icon', 'cube-outline'),
            color=params.get('color', '#3880ff'),
            is_active=True,
        )
        return {"id": str(cat.id), "name": cat.name, "created": True}

    def _create_product(self, params):
        from inventory.models import Product, Category
        name = params.get('name', '')
        if not name:
            raise ValueError("Product name is required")

        price = params.get('price', 0)
        sku = params.get('sku', '')

        product = Product(
            name=name,
            sku=sku,
            price=price,
            cost=params.get('cost', 0),
            description=params.get('description', ''),
            stock=params.get('stock', 0),
            product_type=params.get('product_type', 'physical'),
            is_active=True,
        )
        product.save()

        category_names = params.get('categories', [])
        if category_names:
            matched_cats = []
            for cat_name in category_names:
                cat = Category.objects.filter(name__iexact=cat_name).first()
                if cat:
                    matched_cats.append(cat)
            if matched_cats:
                product.categories.set(matched_cats)

        return {"id": str(product.id), "name": product.name, "sku": product.sku, "created": True}

    # ── Services: Categories & Services ────────────────────────────

    def _create_service_category(self, params):
        from django.utils.text import slugify
        from services.models import ServiceCategory
        name = params.get('name', '')
        if not name:
            raise ValueError("Service category name is required")

        slug = slugify(name)
        existing = ServiceCategory.objects.filter(slug=slug).first()
        if existing:
            return {"message": f"Service category '{name}' already exists", "id": str(existing.id)}

        cat = ServiceCategory.objects.create(
            name=name,
            slug=slug,
            description=params.get('description', ''),
            icon=params.get('icon', ''),
            color=params.get('color', ''),
            is_active=True,
        )
        return {"id": str(cat.id), "name": cat.name, "created": True}

    def _create_service(self, params):
        from django.utils.text import slugify
        from services.models import Service, ServiceCategory
        name = params.get('name', '')
        if not name:
            raise ValueError("Service name is required")

        slug = slugify(name)
        existing = Service.objects.filter(slug=slug).first()
        if existing:
            return {"message": f"Service '{name}' already exists", "id": str(existing.id)}

        category = None
        cat_name = params.get('category')
        if cat_name:
            category = ServiceCategory.objects.filter(name__iexact=cat_name).first()

        svc = Service.objects.create(
            name=name,
            slug=slug,
            price=params.get('price', 0),
            duration_minutes=params.get('duration_minutes', 60),
            pricing_type=params.get('pricing_type', 'fixed'),
            description=params.get('description', ''),
            category=category,
            is_bookable=params.get('is_bookable', True),
            is_active=True,
        )
        return {"id": str(svc.id), "name": svc.name, "created": True}

    # ── Sales: Payment Methods ─────────────────────────────────────

    def _create_payment_method(self, params):
        from sales.models import PaymentMethod
        name = params.get('name', '')
        if not name:
            raise ValueError("Payment method name is required")

        existing = PaymentMethod.objects.filter(name__iexact=name).first()
        if existing:
            return {"message": f"Payment method '{name}' already exists", "id": str(existing.id)}

        pm = PaymentMethod.objects.create(
            name=name,
            type=params.get('payment_type', params.get('type', 'other')),
            is_active=True,
        )
        return {"id": str(pm.id), "name": pm.name, "created": True}

    # ── Schedules: Business Hours ──────────────────────────────────

    def _set_business_hours(self, params):
        from datetime import time
        from schedules.models import BusinessHours
        hours_list = params.get('hours', [])
        if not hours_list and 'day_of_week' in params:
            hours_list = [params]
        if not hours_list:
            raise ValueError("'hours' array is required, or provide 'day_of_week' for a single day")

        updated = []
        for h in hours_list:
            day = h.get('day_of_week')
            if day is None:
                continue
            is_closed = h.get('is_closed', False)
            defaults = {
                'is_closed': is_closed,
                'open_time': h.get('open_time', time(9, 0)),
                'close_time': h.get('close_time', time(18, 0)),
            }
            if 'break_start' in h:
                defaults['break_start'] = h['break_start']
            if 'break_end' in h:
                defaults['break_end'] = h['break_end']

            bh, created = BusinessHours.objects.update_or_create(
                day_of_week=day,
                defaults=defaults,
            )
            updated.append(day)
        return {"updated_days": updated, "success": True}

    # ── Tables (Hospitality) ───────────────────────────────────────

    def _create_zone(self, params):
        from tables.models import Zone
        name = params.get('name', '')
        if not name:
            raise ValueError("Zone name is required")

        existing = Zone.objects.filter(name__iexact=name).first()
        if existing:
            return {"message": f"Zone '{name}' already exists", "id": str(existing.id)}

        zone = Zone.objects.create(
            name=name,
            description=params.get('description', ''),
            color=params.get('color', '#3880ff'),
            is_active=True,
        )
        return {"id": str(zone.id), "name": zone.name, "created": True}

    def _create_table(self, params):
        from tables.models import Table, Zone
        number = params.get('number') or params.get('name')
        if number is None:
            raise ValueError("Table number is required")

        zone = None
        zone_name = params.get('zone')
        if zone_name:
            zone = Zone.objects.filter(name__iexact=zone_name).first()

        existing = Table.objects.filter(number=number).first()
        if existing:
            return {"message": f"Table {number} already exists", "id": str(existing.id)}

        table = Table.objects.create(
            number=number,
            zone=zone,
            capacity=params.get('capacity', 4),
            shape=params.get('shape', 'square'),
            is_active=True,
        )
        return {"id": str(table.id), "number": table.number, "created": True}

    def _bulk_create_zones(self, params):
        """Create multiple zones at once. params: {zones: [{name, description?, color?}]}"""
        zones_data = params.get('zones', [])
        if not zones_data:
            raise ValueError("No zones provided")
        results = []
        for z in zones_data:
            results.append(self._create_zone(z))
        return {"created": len([r for r in results if r.get('created')]), "results": results}

    def _bulk_create_tables(self, params):
        """Create multiple tables at once.
        Supports two formats:
        1. {tables: [{number, zone?, capacity?, shape?}]}
        2. {count: N, start_number?: 1, prefix?: '', zone?: 'name', capacity?: 4, shape?: 'square'}
        """
        tables_data = params.get('tables', [])
        if not tables_data:
            # Support count-based format
            count = params.get('count', 0)
            if count > 0:
                start = params.get('start_number', 1)
                prefix = params.get('prefix', '')
                zone = params.get('zone', '')
                capacity = params.get('capacity', 4)
                shape = params.get('shape', 'square')
                tables_data = [
                    {'number': f"{prefix}{start + i}", 'zone': zone, 'capacity': capacity, 'shape': shape}
                    for i in range(count)
                ]
            else:
                raise ValueError("No tables provided. Use {tables: [...]} or {count: N, zone: 'name'}")
        results = []
        for t in tables_data:
            results.append(self._create_table(t))
        return {"created": len([r for r in results if r.get('created')]), "results": results}
