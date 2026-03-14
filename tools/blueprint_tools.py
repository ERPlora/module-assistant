"""
Blueprint Catalog Tools — search and install products from blueprint catalogs.

These tools allow the AI assistant to browse available seed products
for the hub's business type and install them into inventory.
"""
import logging

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


def _get_hub_config():
    """Return the singleton HubConfig."""
    from apps.configuration.models import HubConfig
    return HubConfig.get_solo()


def _get_hub_business_types():
    """Return the hub's selected business type codes."""
    config = _get_hub_config()
    return getattr(config, 'selected_business_types', []) or []


def _get_hub_country():
    """Return the hub's country code (lowercase), defaulting to 'es'."""
    config = _get_hub_config()
    country = getattr(config, 'country_code', 'es') or 'es'
    return country.lower()


def _get_hub_language():
    """Return the hub's language code, defaulting to 'en'."""
    config = _get_hub_config()
    return getattr(config, 'language', 'en') or 'en'


@register_tool
class SearchBlueprintCatalog(AssistantTool):
    name = "search_blueprint_catalog"
    description = (
        "Search the blueprint product catalog for a business type. "
        "Returns available products with images and prices. "
        "Use this to show the user what seed products are available before installing them."
    )
    short_description = "Search blueprint product catalog by business type. Returns products+categories with prices."
    module_id = None  # core tool, always available
    parameters = {
        "type": "object",
        "properties": {
            "business_type": {
                "type": "string",
                "description": (
                    "Business type code (e.g., 'restaurant', 'cafeteria'). "
                    "If omitted, uses the hub's configured business type."
                ),
            },
            "search": {
                "type": "string",
                "description": "Search term to filter products by name or description.",
            },
            "category": {
                "type": "string",
                "description": "Filter by category code or name.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.core.services.blueprint_service import BlueprintService

        business_type = args.get('business_type', '')
        search = (args.get('search') or '').strip().lower()
        category_filter = (args.get('category') or '').strip().lower()

        # Resolve business type
        type_codes = []
        if business_type:
            type_codes = [business_type]
        else:
            type_codes = _get_hub_business_types()

        if not type_codes:
            return {"error": "No business type specified and hub has no business type configured."}

        country = _get_hub_country()
        language = _get_hub_language()

        all_categories = []
        all_products = []

        for type_code in type_codes:
            data = BlueprintService.get_products(type_code, country=country, language=language)
            if not data:
                continue

            categories = data.get('categories', [])
            products = data.get('products', [])

            # Tag with source type
            for cat in categories:
                cat['_type'] = type_code
            for prod in products:
                prod['_type'] = type_code

            all_categories.extend(categories)
            all_products.extend(products)

        if not all_products and not all_categories:
            return {
                "products": [],
                "categories": [],
                "total": 0,
                "message": f"No product catalog found for {type_codes} in country '{country}'.",
            }

        # Build category code → name map for filtering
        cat_map = {c.get('code', ''): c.get('name', '') for c in all_categories}

        # Apply filters
        filtered = all_products
        if search:
            filtered = [
                p for p in filtered
                if search in (p.get('name') or '').lower()
                or search in (p.get('description') or '').lower()
                or search in (p.get('code') or '').lower()
            ]
        if category_filter:
            filtered = [
                p for p in filtered
                if category_filter in (p.get('category') or '').lower()
                or category_filter in cat_map.get(p.get('category', ''), '').lower()
            ]

        # Build response with clean product data
        result_products = []
        for p in filtered:
            result_products.append({
                'code': p.get('code', ''),
                'name': p.get('name', ''),
                'price': p.get('price', 0),
                'category': p.get('category', ''),
                'category_name': cat_map.get(p.get('category', ''), ''),
                'description': p.get('description', ''),
                'image': p.get('image', ''),
            })

        # Deduplicate categories for response
        seen_codes = set()
        result_categories = []
        for c in all_categories:
            code = c.get('code', '')
            if code and code not in seen_codes:
                seen_codes.add(code)
                result_categories.append({
                    'code': code,
                    'name': c.get('name', ''),
                    'icon': c.get('icon', ''),
                })

        return {
            "products": result_products,
            "categories": result_categories,
            "total": len(result_products),
            "business_types": type_codes,
            "country": country,
        }


@register_tool
class InstallBlueprint(AssistantTool):
    name = "install_blueprint"
    description = (
        "Install a full blueprint for the hub: downloads and installs modules, "
        "creates solution roles, and schedules seed product import. "
        "Use this to set up a hub for a specific business type (e.g., 'restaurant', "
        "'beauty_salon', 'hotel'). After install, the hub will restart to load new modules."
    )
    short_description = "Install modules, roles, and products for a business type. Hub restarts after."
    module_id = None
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "type_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Business type codes to install (e.g., ['beauty_salon'], ['restaurant']). "
                    "Multiple types can be combined (e.g., ['restaurant', 'bar'])."
                ),
            },
            "sector": {
                "type": "string",
                "description": "Optional business sector code (e.g., 'personal_services').",
            },
        },
        "required": ["type_codes"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig
        from apps.core.services.blueprint_service import BlueprintService

        type_codes = args.get('type_codes', [])
        sector = args.get('sector', '')

        if not type_codes:
            return {"error": "type_codes is required (list of business type codes)"}

        hub_config = HubConfig.get_solo()
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
            request.session['assistant_loaded_modules'] = []
            if hasattr(request.session, 'modified'):
                request.session.modified = True

        return {
            "message": f"Blueprint installed for {type_codes}",
            "modules_installed": result.get('modules_installed', 0),
            "roles_created": result.get('roles_created', 0),
            "result": result,
        }


@register_tool
class InstallBlueprintProducts(AssistantTool):
    name = "install_blueprint_products"
    description = (
        "Install products from the blueprint catalog into the inventory. "
        "Can install all products for a business type or a selection by product codes. "
        "This creates categories and products in the local database."
    )
    short_description = "Import seed products from blueprint catalog into local inventory. Use [\"*\"] for all products."
    module_id = None  # core tool, always available
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "business_type": {
                "type": "string",
                "description": (
                    "Business type code (e.g., 'restaurant'). "
                    "If omitted, uses the hub's configured business type."
                ),
            },
            "product_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of product codes to install, or [\"*\"] to install all. "
                    "Use search_blueprint_catalog first to find available codes."
                ),
            },
        },
        "required": ["product_codes"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.core.services.blueprint_service import BlueprintService

        business_type = args.get('business_type', '')
        product_codes = args.get('product_codes', [])

        if not product_codes:
            return {"error": "product_codes is required (list of codes or [\"*\"] for all)."}

        # Resolve business type
        type_codes = []
        if business_type:
            type_codes = [business_type]
        else:
            type_codes = _get_hub_business_types()

        if not type_codes:
            return {"error": "No business type specified and hub has no business type configured."}

        country = _get_hub_country()
        language = _get_hub_language()
        install_all = product_codes == ['*']

        # Check inventory module is available
        try:
            from django.apps import apps
            apps.get_model('inventory', 'Product')
        except LookupError:
            return {"error": "Inventory module is not installed. Install it first."}

        tax_class_mapping = BlueprintService._build_tax_class_mapping()

        imported = 0
        skipped = 0
        categories_created = 0
        errors = []

        for type_code in type_codes:
            data = BlueprintService.get_products(type_code, country=country, language=language)
            if not data:
                errors.append(f"No catalog found for '{type_code}' in country '{country}'.")
                continue

            categories = data.get('categories', [])
            products = data.get('products', [])

            # Import categories, build code → instance map
            category_map = {}
            for cat_data in categories:
                cat, created = BlueprintService._import_category(cat_data, tax_class_mapping)
                if cat:
                    category_map[cat.code] = cat
                    if created:
                        categories_created += 1

            # Filter products if not installing all
            if not install_all:
                codes_set = set(product_codes)
                products = [p for p in products if p.get('code', '') in codes_set]

            # Import products
            for prod_data in products:
                was_imported = BlueprintService._import_product(
                    prod_data,
                    tax_class_mapping=tax_class_mapping,
                    category_map=category_map,
                )
                if was_imported:
                    imported += 1
                else:
                    skipped += 1

        return {
            "success": True,
            "imported": imported,
            "skipped": skipped,
            "categories_created": categories_created,
            "business_types": type_codes,
            "errors": errors,
            "message": (
                f"Installed {imported} products ({skipped} already existed). "
                f"{categories_created} new categories created."
            ),
        }


@register_tool
class ListAvailableCatalogs(AssistantTool):
    name = "list_available_catalogs"
    description = (
        "List business types that have product catalogs available for the hub's country. "
        "Use this to discover which catalogs can be browsed or installed."
    )
    module_id = None  # core tool, always available
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.core.services.blueprint_service import BlueprintService

        country = _get_hub_country()
        language = _get_hub_language()
        hub_types = _get_hub_business_types()

        # Get all business types
        all_types = BlueprintService.get_types(language=language)
        if not all_types:
            return {"error": "Failed to fetch business types from Cloud."}

        # Check which types have product catalogs for this country
        available = []
        for bt in all_types:
            code = bt.get('code', '')
            if not code:
                continue
            data = BlueprintService.get_products(code, country=country, language=language)
            if data and data.get('products'):
                available.append({
                    'code': code,
                    'name': bt.get('name', code),
                    'sector': bt.get('sector', ''),
                    'product_count': len(data.get('products', [])),
                    'category_count': len(data.get('categories', [])),
                    'is_hub_type': code in hub_types,
                })

        return {
            "available_catalogs": available,
            "total": len(available),
            "country": country,
            "hub_business_types": hub_types,
        }
