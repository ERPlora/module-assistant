import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('assistant', '0002_conversation_memory'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssistantFeedback',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
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
                ('sent_to_cloud', models.BooleanField(default=False)),
                ('cloud_error', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
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
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['event_type', 'created_at'], name='assistant_f_event_t_idx'),
                    models.Index(fields=['sent_to_cloud', 'created_at'], name='assistant_f_sent_to_idx'),
                ],
            },
        ),
    ]
