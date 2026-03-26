"""Add missing created_by field to AssistantRequest and AssistantFile.

These models inherit from HubBaseModel which includes created_by,
but the 0003_async_requests migration omitted it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0003_async_requests'),
    ]

    operations = [
        migrations.AddField(
            model_name='assistantrequest',
            name='created_by',
            field=models.UUIDField(blank=True, help_text='UUID of the user who created this record', null=True),
        ),
        migrations.AddField(
            model_name='assistantfile',
            name='created_by',
            field=models.UUIDField(blank=True, help_text='UUID of the user who created this record', null=True),
        ),
    ]
