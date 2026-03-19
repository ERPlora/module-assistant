import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssistantMessage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('hub_id', models.UUIDField(blank=True, db_index=True, editable=False, help_text='Hub this record belongs to (for multi-tenancy)', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.UUIDField(blank=True, help_text='UUID of the user who created this record', null=True)),
                ('updated_by', models.UUIDField(blank=True, help_text='UUID of the user who last updated this record', null=True)),
                ('is_deleted', models.BooleanField(db_index=True, default=False, help_text='Soft delete flag - record is hidden but not removed')),
                ('deleted_at', models.DateTimeField(blank=True, help_text='Timestamp when record was soft deleted', null=True)),
                ('role', models.CharField(choices=[('user', 'User'), ('assistant', 'Assistant')], max_length=10)),
                ('content', models.TextField()),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='assistant.assistantconversation')),
            ],
            options={
                'db_table': 'assistant_assistantmessage',
                'ordering': ['created_at'],
                'abstract': False,
            },
        ),
        migrations.AddIndex(
            model_name='assistantmessage',
            index=models.Index(fields=['conversation', 'created_at'], name='assistant_a_convers_idx'),
        ),
    ]
