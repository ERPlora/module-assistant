from django.contrib import admin
from .models import AssistantConversation, AssistantActionLog


@admin.register(AssistantConversation)
class AssistantConversationAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'context', 'created_at', 'updated_at']
    list_filter = ['context', 'created_at']
    search_fields = ['user__name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AssistantActionLog)
class AssistantActionLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'tool_name', 'success', 'confirmed', 'created_at']
    list_filter = ['success', 'confirmed', 'tool_name', 'created_at']
    search_fields = ['user__name', 'tool_name']
    readonly_fields = ['created_at']
