"""
ExecutePlan — multi-step configuration tool.

Executes a plan (or subset) atomically with a single user confirmation.
Supports: regional config, business info, tax, modules, roles, employees,
categories, products, services, payment methods, business hours, zones,
tables, and blueprint installation.

Step-by-step progress is published to Django cache so the frontend can poll
for live updates during long-running plans.

Rollback: when stop_on_failure=True and a step fails, previously completed
steps are rolled back in reverse order using optional rollback hints stored
per step.
"""
import logging
import os

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)

PLAN_PROGRESS_TIMEOUT = 300  # seconds


def _set_plan_progress(request_id, step_index, total, action, status, message=''):
    """
    Publish per-step progress to cache so the frontend polling loop can show it.

    status: 'running' | 'done' | 'failed' | 'rolling_back' | 'rolled_back'
    """
    if not request_id:
        return
    from django.core.cache import cache
    cache.set(
        f'assistant_progress_{request_id}',
        {
            'type': 'tool',
            'data': message or f'Step {step_index}/{total}: {action}... ({status})',
        },
        timeout=PLAN_PROGRESS_TIMEOUT,
    )


@register_tool
class ExecutePlan(AssistantTool):
    name = "execute_plan"
    strict = False  # params is free-form, can't use strict mode
    description = (
        "Execute a business configuration plan (or subset) atomically. "
        "Takes a list of steps, each with an 'action' and 'params'. "
        "Supported actions: set_regional_config, set_business_info, set_tax_config, "
        "enable_module, disable_module, create_role, create_employee, "
        "create_tax_class, update_store_config, complete_setup, "
        "create_category, create_product, create_service_category, create_service, "
        "create_payment_method, set_business_hours, create_zone, create_table, "
        "bulk_create_zones, bulk_create_tables, bulk_set_business_hours, install_blueprint, "
        "install_blueprint_products, create_station. "
        "IMPORTANT: create_product accepts 'categories' (list of category names) to assign the product to categories. "
        "Always include 'categories' when creating products so they are properly categorized. "
        "Create categories first (create_category), then reference them by name in create_product. "
        "Steps are executed in order with real-time progress updates. "
        "If stop_on_failure=true (default) and a step fails, remaining steps are skipped and "
        "previously completed steps are rolled back in reverse order where rollback info is provided. "
        "Each step may include rollback_action and rollback_params to enable rollback on failure. "
        "Use this after presenting a plan to the user, or for partial execution "
        "(e.g., just installing modules or just creating roles). "
        "CRITICAL: When the user confirms a plan you presented, the steps in execute_plan "
        "MUST match EXACTLY what you described — same names, same prices, same quantities. "
        "Never substitute generic or simplified data for the specific details you showed the user."
    )
    short_description = (
        "Execute a multi-step business configuration plan atomically with real-time progress. "
        "Actions: set_regional_config, set_business_info, set_tax_config, create_role, create_employee, "
        "create_tax_class, update_store_config, complete_setup, create_category, create_product, "
        "create_service_category, create_service, create_payment_method, set_business_hours, "
        "create_zone, create_table, bulk_create_zones, bulk_create_tables, install_blueprint, "
        "install_blueprint_products, create_station. "
        "stop_on_failure=true (default): stops on first error and rolls back completed steps. "
        "ALWAYS use exact names/prices/quantities you showed the user — never substitute."
    )
    requires_confirmation = True
    required_permission = None
    examples = [
        {"steps": [
            {"action": "set_regional_config", "params": {"language": "es", "timezone": "Europe/Madrid", "country_code": "ES", "currency": "EUR"}},
            {"action": "set_business_info", "params": {"business_name": "Salón María", "business_address": "C/ Gran Vía 10, Madrid"}},
            {"action": "set_tax_config", "params": {"tax_rate": 21.0, "tax_included": True}},
            {"action": "create_tax_class", "params": {"name": "IVA General", "rate": 21.0, "is_default": True}},
            {"action": "create_tax_class", "params": {"name": "IVA Reducido", "rate": 10.0, "is_default": False}},
            {"action": "create_category", "params": {"name": "Bebidas"}},
            {"action": "create_product", "params": {"name": "Agua mineral", "price": 2.00, "stock": 50, "categories": ["Bebidas"]}},
            {"action": "create_service_category", "params": {"name": "Cortes"}},
            {"action": "create_employee", "params": {"name": "Ana García", "role_name": "employee", "pin": "1234"}},
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
                        "description": {
                            "type": ["string", "null"],
                            "description": "Optional human-readable description of what this step does (null if not needed)",
                        },
                        "rollback_action": {
                            "type": ["string", "null"],
                            "description": (
                                "Optional action to call to undo this step if a later step fails. "
                                "E.g., 'delete_category' if action was 'create_category'. Null if no rollback needed."
                            ),
                        },
                        "rollback_params": {
                            "type": ["object", "null"],
                            "description": "Parameters for the rollback_action. May reference result fields via {result.field}. Null if no rollback.",
                        },
                    },
                    "required": ["action", "params", "description", "rollback_action", "rollback_params"],
                },
            },
            "stop_on_failure": {
                "type": "boolean",
                "description": (
                    "If true (default), stop executing remaining steps when any step fails "
                    "and roll back previously completed steps in reverse order. "
                    "If false, continue executing remaining steps even after a failure."
                ),
            },
        },
        "required": ["steps", "stop_on_failure"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        steps = args.get('steps', [])
        if not steps:
            return {"success": False, "error": "No steps provided"}

        # stop_on_failure defaults to True — stop & rollback on first error
        stop_on_failure = args.get('stop_on_failure', True)

        # Internal: request_id injected by execute_confirmed_action for live progress
        request_id = args.get('_plan_request_id', None)

        total = len(steps)
        completed = []   # list of (step_dict, step_result) for rollback
        results = []
        errors = []
        failed_step = None
        rolled_back = []

        for i, step in enumerate(steps):
            action = step.get('action', '')
            params = step.get('params', {})
            # Fallback: if LLM omitted 'params' but put fields at step top level,
            # extract them as params (gpt-4o-mini often drops the params wrapper).
            if not params:
                _step_schema_keys = {'action', 'params', 'description', 'rollback_action', 'rollback_params'}
                extra = {k: v for k, v in step.items() if k not in _step_schema_keys}
                if extra:
                    params = extra
            description = step.get('description', '') or action.replace('_', ' ')

            _set_plan_progress(
                request_id, i + 1, total, action, 'running',
                f'Step {i + 1}/{total}: {description}...',
            )

            try:
                result = self._execute_step(action, params, request)
                completed.append((step, result))
                results.append({
                    'step': i + 1,
                    'action': action,
                    'description': description,
                    'success': True,
                    'result': result,
                })
                _set_plan_progress(
                    request_id, i + 1, total, action, 'done',
                    f'Step {i + 1}/{total}: {description} — done',
                )
            except Exception as e:
                logger.error(f"[ASSISTANT] Plan step {i+1} ({action}) failed: {e}", exc_info=True)
                error_msg = self._friendly_error(e)
                failed_step = {
                    'step': i + 1,
                    'action': action,
                    'description': description,
                    'error': error_msg,
                }
                results.append({
                    'step': i + 1,
                    'action': action,
                    'description': description,
                    'success': False,
                    'error': error_msg,
                })
                errors.append(f"Step {i+1} ({action}): {error_msg}")

                _set_plan_progress(
                    request_id, i + 1, total, action, 'failed',
                    f'Step {i + 1}/{total}: {description} — FAILED: {error_msg}',
                )

                if stop_on_failure:
                    # Attempt rollback of completed steps in reverse order
                    for rb_step, rb_result in reversed(completed):
                        rb_action = rb_step.get('rollback_action', '')
                        rb_params = rb_step.get('rollback_params', {})
                        rb_desc = rb_step.get('description', '') or rb_step.get('action', '').replace('_', ' ')

                        if not rb_action:
                            rolled_back.append({
                                'action': rb_step.get('action', ''),
                                'description': rb_desc,
                                'rolled_back': False,
                                'reason': 'No rollback_action defined',
                            })
                            continue

                        # Interpolate result fields into rollback_params
                        resolved_params = self._resolve_rollback_params(rb_params, rb_result)

                        _set_plan_progress(
                            request_id, i + 1, total, rb_action, 'rolling_back',
                            f'Rolling back: {rb_desc}...',
                        )
                        try:
                            self._execute_step(rb_action, resolved_params, request)
                            rolled_back.append({
                                'action': rb_step.get('action', ''),
                                'description': rb_desc,
                                'rolled_back': True,
                            })
                            _set_plan_progress(
                                request_id, i + 1, total, rb_action, 'rolled_back',
                                f'Rolled back: {rb_desc}',
                            )
                        except Exception as rb_exc:
                            rb_error = self._friendly_error(rb_exc)
                            logger.warning(
                                f"[ASSISTANT] Rollback of {rb_step.get('action', '')} failed: {rb_error}"
                            )
                            rolled_back.append({
                                'action': rb_step.get('action', ''),
                                'description': rb_desc,
                                'rolled_back': False,
                                'error': rb_error,
                            })
                    break  # Stop processing remaining steps

        success_count = sum(1 for r in results if r['success'])

        return {
            "success": len(errors) == 0,
            "total_steps": total,
            "succeeded": success_count,
            "failed": len(errors),
            "failed_step": failed_step,
            "results": results,
            "rolled_back": rolled_back,
            "errors": errors,
            "summary": self._build_summary(results, rolled_back, errors),
        }

    def _resolve_rollback_params(self, rb_params, step_result):
        """
        Resolve rollback_params by substituting {result.field} placeholders
        with values from the step's result dict.

        Example: rollback_params={"id": "{result.id}"} + step_result={"id": "42"}
        → {"id": "42"}
        """
        if not rb_params or not isinstance(step_result, dict):
            return rb_params or {}

        import re

        def _substitute(value):
            if not isinstance(value, str):
                return value
            match = re.fullmatch(r'\{result\.(\w+)\}', value.strip())
            if match:
                field = match.group(1)
                return step_result.get(field, value)
            return value

        resolved = {}
        for k, v in rb_params.items():
            resolved[k] = _substitute(v)
        return resolved

    def _build_summary(self, results, rolled_back, errors):
        """Build a concise human-readable summary of plan execution."""
        total = len(results)
        succeeded = sum(1 for r in results if r['success'])
        rb_count = sum(1 for r in rolled_back if r.get('rolled_back'))

        if not errors:
            return f"All {total} steps completed successfully."

        parts = [f"{succeeded}/{total} steps succeeded."]
        if errors:
            parts.append(f"Failed: {errors[0]}")
        if rb_count:
            parts.append(f"Rolled back {rb_count} step(s).")
        elif rolled_back:
            parts.append(f"Rollback attempted for {len(rolled_back)} step(s) (no rollback actions defined).")
        return ' '.join(parts)

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
            'install_blueprint': lambda p: self._install_blueprint(p, request),
            'install_blueprint_products': self._install_blueprint_products,
            'create_station': self._create_station,
        }

        # Common LLM aliases
        aliases = {
            'create_staff_member': 'create_employee',
            'create_staff': 'create_employee',
            'add_employee': 'create_employee',
            'add_staff': 'create_employee',
            'add_service': 'create_service',
            'add_product': 'create_product',
            'add_category': 'create_category',
            'set_business': 'set_business_info',
            'set_config': 'set_regional_config',
            'create_services': 'create_service',
        }
        action = aliases.get(action, action)

        # Unwrap nested params — LLM sometimes wraps as {"parameters": {...actual...}}
        if list(params.keys()) == ['parameters'] and isinstance(params.get('parameters'), dict):
            params = params['parameters']

        handler = dispatch.get(action)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")
        logger.info("[ASSISTANT] Executing action=%s params=%s", action, params)
        return handler(params)

    # ── Error helpers ──────────────────────────────────────────────

    def _friendly_error(self, exc):
        """Convert raw DB/system errors to human-readable messages."""
        msg = str(exc)
        if 'unique constraint' in msg or 'duplicate key' in msg:
            # Try to extract the field that caused the dupe
            import re
            field_match = re.search(r'Key \([\w,\s]+,\s*(\w+)\)=\(', msg)
            if field_match:
                field = field_match.group(1)
                return f"Already exists (duplicate {field})"
            return "Already exists (duplicate value)"
        if 'does not exist' in msg and 'relation' in msg:
            return "Database table not found — module may not be installed"
        if 'ForeignKeyViolation' in type(exc).__name__ or 'ForeignKey' in msg:
            return "Related record not found"
        if 'NOT NULL' in msg or 'null value' in msg:
            return "A required field is missing"
        return msg

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
        # Also accept business type(s) in regional config step
        bt = params.get('selected_business_types') or params.get('business_types') or params.get('business_type')
        if bt:
            if isinstance(bt, str):
                bt = [bt]
            config.selected_business_types = bt
            updated.append('selected_business_types')
        sector = params.get('business_sector') or params.get('sector')
        if sector:
            config.business_sector = sector
            updated.append('business_sector')
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

        # Accept various param shapes the LLM might use
        name = params.get('name', '')
        if not name:
            first = params.get('first_name', '')
            last = params.get('last_name', '')
            name = f"{first} {last}".strip()
        if not name:
            name = params.get('full_name') or params.get('employee_name') or ''
        if not name:
            # Last resort: look for any string value that could be a name
            for key in ('nombre', 'display_name', 'title'):
                if params.get(key):
                    name = params[key]
                    break
        if not name:
            raise ValueError(f"name is required for create_employee (received params: {list(params.keys())})")

        # Check for existing employee by name to avoid duplicates
        existing = LocalUser.objects.filter(hub_id=hub_id, name=name).first()
        if existing:
            return {"message": f"Employee '{name}' already exists", "employee_id": str(existing.id), "name": existing.name}

        role_name = params.get('role_name') or params.get('role') or 'employee'
        role_obj = Role.objects.filter(
            hub_id=hub_id, name=role_name, is_deleted=False,
        ).first()

        import uuid as _uuid
        email = params.get('email', '')
        if not email:
            # Generate unique placeholder to avoid unique constraint on (hub_id, email)
            email = f"{_uuid.uuid4().hex[:8]}@placeholder.local"

        user = LocalUser(
            hub_id=hub_id,
            name=name,
            email=email,
            role=role_name,
            role_obj=role_obj,
        )
        pin = params.get('pin') or params.get('pin_code')
        if pin:
            user.set_pin(str(pin))
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

    def _install_blueprint(self, params, request=None):
        """Install modules from blueprint for given business type codes."""
        from apps.configuration.models import HubConfig
        from apps.core.services.blueprint_service import BlueprintService

        hub_config = HubConfig.get_solo()
        type_codes = params.get('type_codes', [])
        sector = params.get('sector', '')

        # Accept alternative param names the LLM might use
        if not type_codes:
            type_codes = params.get('business_type', [])
            if isinstance(type_codes, str):
                type_codes = [type_codes]
        if not type_codes:
            type_codes = params.get('types', [])
            if isinstance(type_codes, str):
                type_codes = [type_codes]

        # Fallback: use hub's already-configured business types
        if not type_codes:
            type_codes = getattr(hub_config, 'selected_business_types', []) or []

        if not type_codes:
            raise ValueError("type_codes is required (list of business type codes)")

        hub_config.selected_business_types = type_codes
        if sector:
            hub_config.business_sector = sector
        hub_config.save(update_fields=['selected_business_types', 'business_sector'])

        result = BlueprintService.install_blueprint(
            hub_config, type_codes, include_recommended=True,
        )

        # Save post-install state to session so the AI can continue after restart.
        if request is not None and result.get('modules_installed', 0) > 0:
            installed_names = result.get('installed_module_ids', [])
            request.session['assistant_post_install'] = {
                'type_codes': type_codes,
                'modules_installed': installed_names,
                'roles_created': result.get('roles_created', 0),
            }
            request.session['assistant_loaded_modules'] = []  # reset — new modules not yet loaded
            if hasattr(request.session, 'modified'):
                request.session.modified = True

        return {
            "message": f"Blueprint installed for {type_codes}",
            "modules_installed": result.get('modules_installed', 0),
            "roles_created": result.get('roles_created', 0),
            "result": result,
        }

    def _install_blueprint_products(self, params):
        """Install products from blueprint catalog into inventory."""
        from assistant.tools import TOOL_REGISTRY
        tool = TOOL_REGISTRY.get('install_blueprint_products')
        if not tool:
            raise ValueError("install_blueprint_products tool not loaded")
        # Build args matching the tool's parameter schema
        args = {
            'product_codes': params.get('product_codes', ['*']),
        }
        if params.get('business_type'):
            args['business_type'] = params['business_type']
        return tool.execute(args, None)

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

        duration = (
            params.get('duration_minutes')
            or params.get('duration')
            or params.get('duration_min')
            or 60
        )
        svc = Service.objects.create(
            name=name,
            slug=slug,
            price=params.get('price', 0),
            duration_minutes=int(duration),
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
        """Create multiple zones at once. params: {zones: [{name, description?, color?}]} or {zones: ["name1", "name2"]}"""
        zones_data = params.get('zones', [])
        if not zones_data:
            raise ValueError("No zones provided")
        results = []
        for z in zones_data:
            # Accept both string and dict format
            if isinstance(z, str):
                z = {'name': z}
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

    # ── Kitchen Stations ───────────────────────────────────────────

    def _create_station(self, params):
        from orders.models import KitchenStation
        name = params.get('name', '')
        if not name:
            raise ValueError("Station name is required")

        existing = KitchenStation.objects.filter(name__iexact=name).first()
        if existing:
            return {"message": f"Station '{name}' already exists", "id": str(existing.id)}

        station = KitchenStation.objects.create(
            name=name,
            description=params.get('description', ''),
            color=params.get('color', '#F97316'),
            icon=params.get('icon', 'flame-outline'),
            is_active=True,
        )
        return {"id": str(station.id), "name": station.name, "created": True}
