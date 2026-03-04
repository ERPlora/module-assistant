from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('assistant', '0005_migrate_to_hub_base_model'),
    ]

    operations = [
        migrations.RenameField(
            model_name='assistantconversation',
            old_name='openai_response_id',
            new_name='ai_conversation_id',
        ),
    ]
