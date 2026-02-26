"""
P0 Setup Wizard Tools — only available in setup context.

These tools help configure the hub during the initial setup wizard.
"""
from assistant.tools import AssistantTool, register_tool


@register_tool
class SetRegionalConfig(AssistantTool):
    name = "set_regional_config"
    description = "Set regional configuration: language, timezone, country code, currency"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    setup_only = True
    parameters = {
        "type": "object",
        "properties": {
            "language": {
                "type": ["string", "null"],
                "description": "Language code (e.g., 'en', 'es', 'de', 'fr')",
            },
            "timezone": {
                "type": ["string", "null"],
                "description": "Timezone (e.g., 'Europe/Madrid', 'America/New_York')",
            },
            "country_code": {
                "type": ["string", "null"],
                "description": "ISO 3166-1 alpha-2 country code (e.g., 'ES', 'US', 'DE')",
            },
            "currency": {
                "type": ["string", "null"],
                "description": "ISO 4217 currency code (e.g., 'EUR', 'USD', 'GBP')",
            },
        },
        "required": ["language", "timezone", "country_code", "currency"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import HubConfig
        config = HubConfig.get_solo()
        updated = []

        for field in ['language', 'timezone', 'country_code', 'currency']:
            value = args.get(field)
            if value is not None:
                setattr(config, field, value)
                updated.append(field)

        if updated:
            config.save()

        return {"success": True, "updated_fields": updated}


@register_tool
class SetBusinessInfo(AssistantTool):
    name = "set_business_info"
    description = "Set business information: name, address, VAT/tax ID number"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    setup_only = True
    parameters = {
        "type": "object",
        "properties": {
            "business_name": {"type": "string", "description": "Business name"},
            "business_address": {"type": "string", "description": "Business address"},
            "vat_number": {"type": "string", "description": "VAT/Tax ID number (e.g., CIF, NIF, EIN)"},
        },
        "required": ["business_name", "business_address", "vat_number"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        store.business_name = args['business_name']
        store.business_address = args['business_address']
        store.vat_number = args['vat_number']
        store.save()

        return {
            "success": True,
            "business_name": store.business_name,
        }


@register_tool
class SetTaxConfig(AssistantTool):
    name = "set_tax_config"
    description = "Set tax configuration: default tax rate and whether prices include tax"
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    setup_only = True
    parameters = {
        "type": "object",
        "properties": {
            "tax_rate": {"type": "number", "description": "Default tax rate percentage (e.g., 21.0)"},
            "tax_included": {"type": "boolean", "description": "Whether prices include tax by default"},
        },
        "required": ["tax_rate", "tax_included"],
        "additionalProperties": False,
    }

    def execute(self, args, request):
        from apps.configuration.models import StoreConfig
        store = StoreConfig.get_solo()
        store.tax_rate = args['tax_rate']
        store.tax_included = args['tax_included']
        store.is_configured = True
        store.save()

        return {
            "success": True,
            "tax_rate": str(store.tax_rate),
            "tax_included": store.tax_included,
        }


@register_tool
class CompleteSetupStep(AssistantTool):
    name = "complete_setup_step"
    description = "Mark the hub setup as complete. Call this after all configuration steps are done."
    requires_confirmation = True
    required_permission = "assistant.use_setup_mode"
    setup_only = True
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
