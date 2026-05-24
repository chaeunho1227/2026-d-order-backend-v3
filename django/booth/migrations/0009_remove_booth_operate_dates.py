from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('booth', '0008_alter_booth_location'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='booth',
            name='operate_dates',
        ),
    ]
