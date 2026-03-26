from django.urls import path
from . import views

app_name = 'assistant'

urlpatterns = [
    # Pages (GET)
    path('', views.chat_page, name='index'),
    path('chat/', views.chat_page, name='chat'),
    path('history/', views.history_page, name='history'),
    path('logs/', views.logs_page, name='logs'),
    path('plan/', views.plan_page, name='plan'),

    # History HTMX (infinite scroll + search)
    path('history/load/', views.history_load_more, name='history_load_more'),

    # API (POST)
    path('chat/send/', views.chat, name='chat_message'),
    path('chat/stream/', views.chat_stream, name='chat_stream'),
    path('poll/<str:request_id>/', views.poll_progress, name='poll_progress'),
    path('confirm/<str:log_id>/', views.confirm_action, name='confirm_action'),
    path('cancel/<str:log_id>/', views.cancel_action, name='cancel_action'),

    # Messages (paginated, infinite scroll up)
    path('messages/<int:conversation_id>/', views.load_conversation_messages, name='load_conversation_messages'),

    # Files
    path('files/<uuid:file_id>/download/', views.download_file, name='download_file'),

    # Setup
    path('skip-setup/', views.skip_setup, name='skip_setup'),
]
