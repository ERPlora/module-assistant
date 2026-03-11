from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='assistantactionlog',
            name='llm_message_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
