"""
Migrate assistant models from models.Model to HubBaseModel.

Tables are empty so we drop and let Django recreate with the correct schema.
This adds: UUID pk, hub_id, created_by, updated_by, is_deleted, deleted_at,
created_at, updated_at, and db_table names.
"""
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0004_rename_assistant_f_event_t_idx_assistant_a_event_t_90f162_idx_and_more'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        # Drop existing tables (empty, no data loss)
        migrations.RunSQL(
            sql=[
                'DROP TABLE IF EXISTS "assistant_assistantfeedback" CASCADE;',
                'DROP TABLE IF EXISTS "assistant_assistantactionlog" CASCADE;',
                'DROP TABLE IF EXISTS "assistant_assistantconversation" CASCADE;',
            ],
            reverse_sql=[],  # No reverse — forward-only migration
        ),

        # Recreate AssistantConversation with HubBaseModel
        migrations.CreateModel(
            name='AssistantConversation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('hub_id', models.UUIDField(db_index=True)),
                ('created_by', models.UUIDField(blank=True, null=True)),
                ('updated_by', models.UUIDField(blank=True, null=True)),
                ('is_deleted', models.BooleanField(db_index=True, default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assistant_conversations',
                    to='accounts.localuser',
                )),
                ('openai_response_id', models.CharField(blank=True, default='', max_length=255)),
                ('context', models.CharField(default='general', max_length=50)),
                ('title', models.CharField(blank=True, default='', max_length=200)),
                ('summary', models.TextField(blank=True, default='')),
                ('first_message', models.TextField(blank=True, default='')),
                ('message_count', models.PositiveIntegerField(default=0)),
            ],
            options={
                'db_table': 'assistant_assistantconversation',
                'ordering': ['-updated_at'],
                'abstract': False,
            },
        ),

        # Recreate AssistantActionLog with HubBaseModel
        migrations.CreateModel(
            name='AssistantActionLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('hub_id', models.UUIDField(db_index=True)),
                ('created_by', models.UUIDField(blank=True, null=True)),
                ('updated_by', models.UUIDField(blank=True, null=True)),
                ('is_deleted', models.BooleanField(db_index=True, default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assistant_action_logs',
                    to='accounts.localuser',
                )),
                ('conversation', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='action_logs',
                    to='assistant.assistantconversation',
                )),
                ('tool_name', models.CharField(max_length=100)),
                ('tool_args', models.JSONField(default=dict)),
                ('result', models.JSONField(default=dict)),
                ('success', models.BooleanField(default=False)),
                ('confirmed', models.BooleanField(default=False)),
                ('error_message', models.TextField(blank=True, default='')),
            ],
            options={
                'db_table': 'assistant_assistantactionlog',
                'ordering': ['-created_at'],
                'abstract': False,
            },
        ),

        # Recreate AssistantFeedback with HubBaseModel
        migrations.CreateModel(
            name='AssistantFeedback',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('hub_id', models.UUIDField(db_index=True)),
                ('created_by', models.UUIDField(blank=True, null=True)),
                ('updated_by', models.UUIDField(blank=True, null=True)),
                ('is_deleted', models.BooleanField(db_index=True, default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event_type', models.CharField(
                    choices=[
                        ('tool_error', 'Tool Error'),
                        ('zero_results', 'Zero Results'),
                        ('missing_feature', 'Missing Feature'),
                    ],
                    max_length=30,
                )),
                ('tool_name', models.CharField(blank=True, default='', max_length=100)),
                ('user_message', models.TextField(blank=True, default='')),
                ('details', models.JSONField(default=dict)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assistant_feedback',
                    to='accounts.localuser',
                )),
                ('conversation', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='feedback_events',
                    to='assistant.assistantconversation',
                )),
                ('action_log', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='feedback_events',
                    to='assistant.assistantactionlog',
                )),
                ('sent_to_cloud', models.BooleanField(default=False)),
                ('cloud_error', models.CharField(blank=True, default='', max_length=255)),
            ],
            options={
                'db_table': 'assistant_assistantfeedback',
                'ordering': ['-created_at'],
                'abstract': False,
                'indexes': [
                    models.Index(fields=['event_type', 'created_at'], name='assistant_a_event_t_90f162_idx'),
                    models.Index(fields=['sent_to_cloud', 'created_at'], name='assistant_a_sent_to_a43d79_idx'),
                ],
            },
        ),
    ]
