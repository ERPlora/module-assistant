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

    # Core actions always available (not dependent on module installation)
    CORE_ACTIONS = [
        'set_regional_config', 'set_business_info', 'set_tax_config',
        'enable_module', 'disable_module', 'create_role', 'create_employee',
        'create_tax_class', 'update_store_config', 'complete_setup',
        'install_blueprint',
    ]

    @classmethod
    def _get_available_actions(cls):
        """Build list of available actions dynamically from core + registered tools."""
        from assistant.tools import TOOL_REGISTRY
        actions = list(cls.CORE_ACTIONS)
        # Add module tool names that are currently registered
        for name in TOOL_REGISTRY:
            if name not in actions and name != 'execute_plan':
                actions.append(name)
        return actions

    @property
    def description(self):
        actions = ', '.join(self._get_available_actions())
        return (
            "Execute a business configuration plan (or subset) atomically. "
            "Takes a list of steps, each with an 'action' and 'params'. "
            f"Available actions: {actions}. "
            "IMPORTANT: create_tax_class REQUIRES 'rate' (number) in params. "
            "Example: {\"action\": \"create_tax_class\", \"params\": {\"name\": \"IVA General\", \"rate\": 21.0, \"is_default\": true}}. "
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

    @property
    def short_description(self):
        actions = ', '.join(self._get_available_actions())
        return (
            "Execute a multi-step business configuration plan atomically with real-time progress. "
            f"Available actions: {actions}. "
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
            params = step.get('params') or {}
            # Fallback: if LLM omitted 'params' but put fields at step top level,
            # extract them as params (LLM sometimes drops the params wrapper).
            if not params:
                _step_schema_keys = {'action', 'params', 'description', 'rollback_action', 'rollback_params'}
                extra = {k: v for k, v in step.items() if k not in _step_schema_keys}
                if extra:
                    params = extra
            # Last resort: parse description to build params for known actions
            if not params:
                params = self._params_from_description(action, step.get('description', ''))
            description = step.get('description', '') or action.replace('_', ' ')

            _set_plan_progress(
                request_id, i + 1, total, action, 'running',
                f'Step {i + 1}/{total}: {description}...',
            )

            try:
                result = self._execute_step(action, params, request, description)
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

        # Auto-complete setup if plan succeeded and hub is not yet configured
        if len(errors) == 0 and success_count > 0:
            try:
                from apps.configuration.models import HubConfig, StoreConfig
                hub_config = HubConfig.get_solo()
                store_config = StoreConfig.get_solo()
                if not hub_config.is_configured or not store_config.is_configured:
                    hub_config.is_configured = True
                    hub_config.save(update_fields=['is_configured'])
                    store_config.is_configured = True
                    store_config.save(update_fields=['is_configured'])
                    logger.info("[ASSISTANT] Auto-completed setup after successful plan execution")
            except Exception as exc:
                logger.warning("[ASSISTANT] Auto-complete setup failed: %s", exc)

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

    def _execute_step(self, action, params, request, description=''):
        """Execute a single plan step by delegating to TOOL_REGISTRY.

        Hub core actions (settings, tax, modules) are handled inline.
        All module-specific actions delegate to the module's registered tool.
        """
        from assistant.tools import get_tool

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

        logger.info("[ASSISTANT] Executing action=%s params=%s", action, params)

        # Hub core actions — these are NOT module tools, they live in configure_tools
        core_dispatch = {
            'set_regional_config': self._set_regional_config,
            'set_business_info': self._set_business_info,
            'set_tax_config': self._set_tax_config,
            'enable_module': self._enable_module,
            'disable_module': self._disable_module,
            'create_tax_class': self._create_tax_class,
            'update_store_config': self._update_store_config,
            'complete_setup': lambda p: self._complete_setup(),
            'install_blueprint': lambda p: self._install_blueprint(p, request),
        }

        handler = core_dispatch.get(action)
        if handler is not None:
            return handler(params)

        # Everything else → delegate to module tools via TOOL_REGISTRY
        tool = get_tool(action)
        if tool is None:
            # Tools may not be registered yet if modules were installed after
            # initial discover_tools() ran. Force re-discovery and retry once.
            from assistant.tools import discover_tools
            discover_tools()
            tool = get_tool(action)
        if tool is not None:
            return tool.safe_execute(params, request)

        raise ValueError(f"Unknown action: {action}. No core handler or registered tool found.")

    # ── Param recovery helpers ────────────────────────────────────

    def _params_from_description(self, action, description):
        """
        Last-resort recovery: when LLM puts all info in the description
        instead of params, try to extract structured data.
        Pattern seen: "Crear rol 'chef' con permisos: sales.view_*, inventory.view_*"
        """
        import re
        if not description:
            return {}

        if action == 'create_role':
            # Try to extract role name from quotes or after "rol"
            name_match = (
                re.search(r"['\"](\w[\w\s]*?)['\"]", description)
                or re.search(r"rol\s+(\w+)", description, re.IGNORECASE)
            )
            name = name_match.group(1).strip() if name_match else ''
            # Extract wildcard patterns (word.word_* or word.*)
            wildcards = re.findall(r'\b(\w+\.(?:\w+_)?\*)', description)
            if name:
                return {'name': name, 'wildcards': wildcards}

        if action == 'create_employee':
            # "Crear empleado 'Name' con rol X y PIN 1234"
            name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
            name = name_match.group(1).strip() if name_match else ''
            role_match = re.search(r"rol\s+['\"]?(\w+)['\"]?", description, re.IGNORECASE)
            role_name = role_match.group(1) if role_match else 'employee'
            pin_match = re.search(r"PIN\s+(\d{4,})", description, re.IGNORECASE)
            pin = pin_match.group(1) if pin_match else ''
            if name:
                return {'name': name, 'role_name': role_name, 'pin': pin}

        if action == 'install_blueprint':
            # Extract type_codes from description like "install blueprint (restaurant)"
            # or "Install restaurant blueprint" or "blueprint for restaurant"
            from apps.configuration.models import HubConfig
            desc_lower = description.lower()
            # Try to find known business type codes in the description
            known_types = [
                'restaurant', 'beauty_salon', 'hotel', 'dental_clinic',
                'academy', 'rental', 'accounting_firm', 'law_firm',
                'real_estate', 'manufacturing', 'travel_agency',
                'software_company', 'tobacco_shop', 'bar', 'cafe',
                'bakery', 'gym', 'spa', 'veterinary', 'pharmacy',
            ]
            found = [t for t in known_types if t in desc_lower]
            if not found:
                # Fallback: use hub config's selected types
                hub_config = HubConfig.get_solo()
                found = getattr(hub_config, 'selected_business_types', []) or []
            if found:
                return {'type_codes': found}

        if action == 'create_tax_class':
            # "IVA General (21%)" or "Create tax class 'IVA Reducido' at 10%"
            # Also handles: "Crear clase: IVA General", "Crear clase de IVA Exento"
            rate_match = re.search(r'(\d+(?:\.\d+)?)\s*%', description)
            if rate_match:
                rate = float(rate_match.group(1))
                # Extract name: everything before the rate, or quoted text
                name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
                if name_match:
                    name = name_match.group(1).strip()
                else:
                    # Use text before the percentage as name
                    name = description.split(str(rate_match.group(1)))[0].strip(' -(')
                    if not name or len(name) < 2:
                        name = f"Tax {rate}%"
                return {'name': name, 'rate': rate}
            # Fallback: known Spanish tax class names without % in description
            _tax_map = {
                'general': 21, 'reducido': 10, 'superreducido': 4, 'exento': 0,
            }
            desc_lower = description.lower()
            for tax_name, tax_rate in _tax_map.items():
                if tax_name in desc_lower:
                    return {'name': f"IVA {tax_name.capitalize()}", 'rate': tax_rate}

        if action == 'create_category':
            # "Crear categoría 'Tabaco'" or "Create category 'Lotería' icon leaf-outline"
            name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
            if name_match:
                result = {'name': name_match.group(1).strip()}
                icon_match = re.search(r"icon\s+['\"]?(\w[\w-]*)['\"]?", description, re.IGNORECASE)
                if icon_match:
                    result['icon'] = icon_match.group(1)
                color_match = re.search(r"color\s+['\"]?(#[0-9a-fA-F]{6})['\"]?", description, re.IGNORECASE)
                if color_match:
                    result['color'] = color_match.group(1)
                return result
            # Fallback: use everything after "category" or "categoría" as the name
            fallback = re.search(r"(?:category|categoría|categoria)\s+(.+)", description, re.IGNORECASE)
            if fallback:
                return {'name': fallback.group(1).strip().strip("'\"").strip()}

        if action in ('bulk_create_zones', 'create_zone'):
            # "Terraza" or "Create zone 'Terraza'"
            name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
            if name_match:
                return {'name': name_match.group(1).strip()}

        if action in ('bulk_create_tables', 'create_table'):
            # "8 tables in zone 'Terraza' capacity 4" or "Create 12 tables for Interior (cap 6)"
            count_match = re.search(r'(\d+)\s*(?:tables|mesas)', description, re.IGNORECASE)
            zone_match = re.search(r"(?:zone|zona)\s+['\"]?([^'\"]+?)['\"]?(?:\s|,|$)", description, re.IGNORECASE)
            cap_match = re.search(r"(?:capacity|capacidad|cap)\s*(\d+)", description, re.IGNORECASE)
            result = {}
            if count_match:
                result['count'] = int(count_match.group(1))
            if zone_match:
                result['zone_id'] = zone_match.group(1).strip()
            if cap_match:
                result['capacity'] = int(cap_match.group(1))
            if result:
                return result

        if action == 'create_course':
            name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
            if name_match:
                result = {'name': name_match.group(1).strip()}
                price_match = re.search(r'(\d+(?:\.\d+)?)\s*€', description)
                if price_match:
                    result['price'] = price_match.group(1)
                return result

        if action == 'create_product':
            # "Martillo percutor 25€" or "Create product 'Silla plegable' price 5€"
            name_match = re.search(r"['\"]([^'\"]+)['\"]", description)
            if not name_match:
                # Try everything before the price as the name
                price_match = re.search(r'(\d+(?:[.,]\d+)?)\s*€', description)
                if price_match:
                    name = description[:price_match.start()].strip(' -–—:')
                    if name:
                        return {'name': name, 'price': price_match.group(1).replace(',', '.')}
            else:
                result = {'name': name_match.group(1).strip()}
                price_match = re.search(r'(\d+(?:[.,]\d+)?)\s*€', description)
                if price_match:
                    result['price'] = price_match.group(1).replace(',', '.')
                return result

        if action in ('import_seeds', 'import_products'):
            # "Import restaurant products" or "Importar productos de restaurante"
            from apps.configuration.models import HubConfig
            hub_config = HubConfig.get_solo()
            country = getattr(hub_config, 'country_code', 'es') or 'es'
            types = getattr(hub_config, 'selected_business_types', []) or []
            if types:
                return {'type_code': types[0], 'country': country}

        if action == 'update_store_config':
            # Try to extract key=value pairs from description
            result = {}
            name_match = re.search(r"(?:nombre|name)[:\s]+['\"]?([^'\"]+?)['\"]?(?:,|$)", description, re.IGNORECASE)
            if name_match:
                result['business_name'] = name_match.group(1).strip()
            addr_match = re.search(r"(?:dirección|address|direcci[oó]n)[:\s]+['\"]?([^'\"]+?)['\"]?(?:,|$)", description, re.IGNORECASE)
            if addr_match:
                result['business_address'] = addr_match.group(1).strip()
            cif_match = re.search(r"(?:CIF|NIF|VAT|vat_number)[:\s]+['\"]?([A-Z0-9]+)['\"]?", description, re.IGNORECASE)
            if cif_match:
                result['vat_number'] = cif_match.group(1).strip()
            phone_match = re.search(r"(?:teléfono|phone|tel)[:\s]+['\"]?(\d[\d\s]+)['\"]?", description, re.IGNORECASE)
            if phone_match:
                result['phone'] = phone_match.group(1).strip()
            email_match = re.search(r"(?:email|correo)[:\s]+['\"]?([\w.@+-]+)['\"]?", description, re.IGNORECASE)
            if email_match:
                result['email'] = email_match.group(1).strip()
            rate_match = re.search(r"(?:tax_rate|iva)[:\s]+(\d+(?:\.\d+)?)", description, re.IGNORECASE)
            if rate_match:
                result['tax_rate'] = float(rate_match.group(1))
            if 'tax_included' in description.lower() or 'iva incluido' in description.lower():
                result['tax_included'] = True
            if result:
                return result

        return {}

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
        from decimal import Decimal
        from apps.configuration.models import StoreConfig, TaxClass
        store = StoreConfig.get_solo()
        # Accept multiple key names for tax rate
        tax_rate = None
        for key in ('tax_rate', 'default_tax_rate', 'rate', 'percentage', 'tax_percentage', 'rate_percent', 'default_rate'):
            if key in params:
                tax_rate = params[key]
                break
        if tax_rate is not None:
            store.tax_rate = tax_rate
            # Auto-link default_tax_class if a TaxClass matches the tax_rate
            rate = Decimal(str(tax_rate))
            matching_tc = TaxClass.objects.filter(rate=rate).first()
            if matching_tc:
                store.default_tax_class = matching_tc
        if 'tax_included' in params:
            store.tax_included = params['tax_included']
        store.is_configured = True
        store.save()
        result = {"tax_rate": str(store.tax_rate), "tax_included": store.tax_included}
        if store.default_tax_class:
            result["default_tax_class"] = store.default_tax_class.name
        return result

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

    # ── Tax ────────────────────────────────────────────────────────

    def _create_tax_class(self, params):
        import re as _re
        from decimal import Decimal
        from apps.configuration.models import TaxClass

        name = (params.get('name') or params.get('tax_name') or
                params.get('label') or params.get('title'))
        # Accept rate from multiple param names — AI often uses different keys
        rate = None
        for key in ('rate', 'tax_rate', 'percentage', 'value', 'tax_percentage', 'rate_percent', 'tax_value', 'percent'):
            if key in params and params[key] is not None:
                rate = params[key]
                break
        if rate is None:
            raise ValueError(
                f"Tax rate is required. Provide 'rate' in params. "
                f"Received params: {list(params.keys())}"
            )
        rate = Decimal(str(float(rate)))

        # Clean name: strip common LLM prefixes like "Crear clase: ", "Crear clase de "
        if name:
            name = _re.sub(
                r'^(?:crear?\s+)?(?:clase\s+(?:de\s+)?)?(?:iva\s+)?',
                '', name, flags=_re.IGNORECASE,
            ).strip(' :-(')
            # Re-add "IVA " prefix for Spanish tax classes if it was stripped
            if name and not name.upper().startswith('IVA'):
                name = f"IVA {name}"
        if not name:
            name = f"Tax {rate}%"

        is_default = params.get('is_default', False)

        # Duplicate check: skip if same name or same rate already exists
        if TaxClass.objects.filter(name=name).exists():
            existing = TaxClass.objects.get(name=name)
            return {"tax_class_id": existing.id, "name": existing.name,
                    "rate": str(existing.rate), "already_exists": True}
        if TaxClass.objects.filter(rate=rate).exists():
            existing = TaxClass.objects.filter(rate=rate).first()
            return {"tax_class_id": existing.id, "name": existing.name,
                    "rate": str(existing.rate), "already_exists": True}

        if is_default:
            TaxClass.objects.filter(is_default=True).update(is_default=False)

        order = TaxClass.objects.count() + 1
        tc = TaxClass.objects.create(
            name=name,
            rate=rate,
            description=params.get('description', ''),
            is_default=is_default,
            order=order,
        )

        # Auto-link default_tax_class on StoreConfig if rate matches store.tax_rate
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        if store.tax_rate and not store.default_tax_class_id and Decimal(str(store.tax_rate)) == rate:
            store.default_tax_class = tc
            store.save(update_fields=['default_tax_class'])

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
        for key in ('business_type', 'types', 'type', 'business_types',
                     'blueprint', 'blueprint_type', 'business_type_code',
                     'business_type_codes', 'code', 'codes'):
            if not type_codes:
                val = params.get(key, [])
                if isinstance(val, str):
                    val = [val]
                if val:
                    type_codes = val

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

        # Refresh tool registry so newly installed module tools are available
        if result.get('modules_installed', 0) > 0:
            from assistant.tools import discover_tools
            discover_tools()

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

    # ── Module-specific actions (DELEGATED to module ai_tools) ──
    # All module-specific actions are now handled by _execute_step's
    # TOOL_REGISTRY delegation. The following are delegated:
    #
    # create_role → setup_tools.py:CreateRole
    # create_employee → setup_tools.py:CreateEmployee
    # bulk_create_employees → setup_tools.py:BulkCreateEmployees
    # create_zone → tables/ai_tools.py:CreateZone
    # create_table → tables/ai_tools.py:CreateTable
    # bulk_create_zones → tables/ai_tools.py:BulkCreateZones
    # bulk_create_tables → tables/ai_tools.py:BulkCreateTables
    # create_category → inventory/ai_tools.py:CreateCategory
    # create_product → inventory/ai_tools.py:CreateProduct
    # create_service_category → services/ai_tools.py:CreateServiceCategory
    # create_service → services/ai_tools.py:CreateService
    # create_payment_method → sales/ai_tools.py:CreatePaymentMethod
    # set_business_hours → schedules/ai_tools.py:SetBusinessHours
    # bulk_set_business_hours → schedules/ai_tools.py:BulkSetBusinessHours
    # create_station → kitchen/ai_tools.py:CreateStation
    # install_blueprint_products → blueprint_tools.py:InstallBlueprintProducts
