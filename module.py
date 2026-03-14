from django.utils.translation import gettext_lazy as _

MODULE_ID = 'assistant'
MODULE_NAME = _('AI Assistant')
MODULE_DESCRIPTION = _(
    'AI-powered business assistant with contextual tools for inventory, sales, '
    'customers, invoicing, and more. Supports voice input and tiered subscription '
    'plans (Basic, Pro, Enterprise) with different AI models and usage limits.'
)
MODULE_VERSION = '1.0.6'
MODULE_AUTHOR = 'ERPlora'
MODULE_ICON = 'sparkles-outline'
MODULE_FUNCTIONS = ['utility', 'ai']
MODULE_COLOR = '#7c3aed'

COMPATIBILITY = {
    'min_erplora_version': '1.0.0',
}

MENU = {
    'label': _('AI Assistant'),
    'icon': 'sparkles-outline',
    'order': 99,
}

NAVIGATION = [
    {'label': _('Chat'), 'icon': 'chatbubbles-outline', 'id': 'chat'},
    {'label': _('History'), 'icon': 'time-outline', 'id': 'history'},
    {'label': _('Action Log'), 'icon': 'list-outline', 'id': 'logs'},
]

PERMISSIONS = [
    'assistant.use_chat',
    'assistant.use_setup_mode',
    'assistant.view_logs',
    'assistant.manage_settings',
]

ROLE_PERMISSIONS = {
    "admin": ["*"],
    "manager": [
        "use_chat",
        "use_setup_mode",
        "view_logs",
    ],
    "employee": [
        "use_chat",
        "use_setup_mode",
        "view_logs",
    ],
}
