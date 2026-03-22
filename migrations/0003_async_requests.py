import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('assistant', '0002_assistantmessage'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssistantRequest',
            fields=[
                ('hub_id', models.CharField(db_index=True, default='default', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('cloud_request_id', models.UUIDField(blank=True, help_text='AssistantAsyncRequest ID in Cloud DB', null=True)),
                ('user_message', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('complete', 'Complete'), ('error', 'Error')], default='pending', max_length=20)),
                ('progress_message', models.CharField(blank=True, default='', max_length=200)),
                ('response_text', models.TextField(blank=True, default='')),
                ('pending_actions', models.JSONField(blank=True, default=list)),
                ('error_message', models.TextField(blank=True, default='')),
                ('is_seen', models.BooleanField(default=False)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='requests', to='assistant.assistantconversation')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assistant_requests', to='accounts.localuser')),
            ],
            options={
                'db_table': 'assistant_assistantrequest',
                'ordering': ['-created_at'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='AssistantFile',
            fields=[
                ('hub_id', models.CharField(db_index=True, default='default', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(help_text='Original filename (e.g. products_export.csv)', max_length=255)),
                ('s3_key', models.CharField(help_text='S3 object key', max_length=500)),
                ('file_type', models.CharField(blank=True, default='', max_length=50)),
                ('size_bytes', models.PositiveIntegerField(default=0)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='files', to='assistant.assistantconversation')),
                ('request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='files', to='assistant.assistantrequest')),
            ],
            options={
                'db_table': 'assistant_assistantfile',
                'ordering': ['-created_at'],
                'abstract': False,
            },
        ),
        migrations.AddIndex(
            model_name='assistantrequest',
            index=models.Index(fields=['user', 'status'], name='assistant_a_user_id_status_idx'),
        ),
        migrations.AddIndex(
            model_name='assistantrequest',
            index=models.Index(fields=['conversation', '-created_at'], name='assistant_a_conv_created_idx'),
        ),
    ]
