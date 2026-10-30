from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Profile = apps.get_model("accounts", "Profile")

    existing_user_ids = set(Profile.objects.values_list("user_id", flat=True))
    missing = []
    for user in User.objects.all():
        if user.id in existing_user_ids:
            continue
        # Preserve existing permissions: anyone who was already staff keeps
        # read & write access under the new Profile model.
        level = "write" if user.is_staff else "member"
        missing.append(Profile(user_id=user.id, access_level=level))

    if missing:
        Profile.objects.bulk_create(missing)


def remove_profiles(apps, schema_editor):
    # Nothing to undo: dropping the table is handled by the previous migration.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_missing_profiles, remove_profiles),
    ]
