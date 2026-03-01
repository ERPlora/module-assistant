from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='assistantconversation',
            name='title',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='assistantconversation',
            name='summary',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='assistantconversation',
            name='first_message',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='assistantconversation',
            name='message_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
