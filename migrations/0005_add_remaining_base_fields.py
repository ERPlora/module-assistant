"""Add remaining HubBaseModel fields to AssistantRequest and AssistantFile.

0004 only added created_by. This adds the rest: updated_by, is_deleted, deleted_at.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0004_add_missing_created_by'),
    ]

    operations = [
        # AssistantRequest
        migrations.AddField(
            model_name='assistantrequest',
            name='updated_by',
            field=models.UUIDField(blank=True, help_text='UUID of the user who last updated this record', null=True),
        ),
        migrations.AddField(
            model_name='assistantrequest',
            name='is_deleted',
            field=models.BooleanField(db_index=True, default=False, help_text='Soft delete flag - record is hidden but not removed'),
        ),
        migrations.AddField(
            model_name='assistantrequest',
            name='deleted_at',
            field=models.DateTimeField(blank=True, help_text='Timestamp when record was soft deleted', null=True),
        ),
        # AssistantFile
        migrations.AddField(
            model_name='assistantfile',
            name='updated_by',
            field=models.UUIDField(blank=True, help_text='UUID of the user who last updated this record', null=True),
        ),
        migrations.AddField(
            model_name='assistantfile',
            name='is_deleted',
            field=models.BooleanField(db_index=True, default=False, help_text='Soft delete flag - record is hidden but not removed'),
        ),
        migrations.AddField(
            model_name='assistantfile',
            name='deleted_at',
            field=models.DateTimeField(blank=True, help_text='Timestamp when record was soft deleted', null=True),
        ),
    ]
