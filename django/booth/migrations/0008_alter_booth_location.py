from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('booth', '0007_alter_booth_operate_dates_default'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                # 1. 임시 jsonb 컬럼 추가
                migrations.RunSQL(
                    sql="ALTER TABLE booth_booth ADD COLUMN location_new jsonb DEFAULT '{}';",
                    reverse_sql="ALTER TABLE booth_booth DROP COLUMN IF EXISTS location_new;",
                ),
                # 2. 기존 location 문자열을 operate_dates의 각 날짜를 키로 하는 JSON 객체로 변환
                migrations.RunSQL(
                    sql="""
                        UPDATE booth_booth
                        SET location_new = (
                            SELECT COALESCE(jsonb_object_agg(d, location), '{}')
                            FROM jsonb_array_elements_text(operate_dates) AS d
                        )
                        WHERE location IS NOT NULL AND location != '';
                    """,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                # 3. 기존 location 컬럼 제거 후 임시 컬럼 이름 변경
                migrations.RunSQL(
                    sql="""
                        ALTER TABLE booth_booth DROP COLUMN location;
                        ALTER TABLE booth_booth RENAME COLUMN location_new TO location;
                    """,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name='booth',
                    name='location',
                    field=models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="날짜별 부스 위치 (예: {'2026-05-23': 'A구역'})",
                    ),
                ),
            ],
        ),
    ]
