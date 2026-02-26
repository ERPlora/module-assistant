from django.utils.translation import gettext_lazy as _

MODULE_ID = 'assistant'
MODULE_NAME = _('AI Assistant')
MODULE_VERSION = '1.0.0'
MODULE_ICON = 'sparkles-outline'

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
