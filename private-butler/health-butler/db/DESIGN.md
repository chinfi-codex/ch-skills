# Health Butler SQLite Data Design

## Scope

This design covers the current local health-butler data only:

- Long-lived personal profile and health goals
- Daily body snapshot: weight, body fat, BMI, target weight
- Daily meals and meal food items
- Daily water intake
- Daily medication records
- Daily exercise records and exercise items
- Daily health markers: sleep, kegel, symptoms, energy
- Daily notes

## Storage Direction

SQLite should become the source of truth. JSON files can remain as an export format for the existing static dashboard.

Recommended data flow:

```text
health.db
  -> export daily dashboard JSON
  -> build dashboard/index.html
```

The dashboard should not need to query SQLite directly. Keeping the browser view static preserves the current easy-open workflow and avoids requiring a local web service.

## Skill Runtime Layout

The skill is self-contained and should resolve files relative to the skill root:

- Main instructions: `SKILL.md`
- Database schema: `db/schema.sql`
- Runtime database: `health.db`
- Dashboard output: `dashboard/index.html`

After migration, any agent handling health records should:

1. Read `db/DESIGN.md` and `db/schema.sql` before changing storage behavior.
2. Prefer `health.db` for reads and writes.
3. Treat `data/daily/*.json`, `daily-log.md`, and `dashboard/index.html` as generated or compatibility outputs.
4. Use JSON only as a fallback while `health.db` or DB write scripts are not available.
5. Rebuild dashboard outputs after data changes.

## Design Principles

1. Store facts separately from summaries.
2. Make daily totals reproducible from detailed records.
3. Keep one row per real-world event or daily observation.
4. Keep human notes, planned items, and completed items in the same model with explicit status.
5. Keep old JSON export compatibility until the dashboard is rewritten.

## Entity Overview

### profiles

One row for the current person using the system. All profile data lives here, no external JSON file.

Fields:

- age, gender, height_cm
- weight_kg, target_weight_kg, bmi (auto-updated when weight is recorded)
- bmr
- diet_preference, exercise_level
- sleep_bedtime_pref, sleep_wake_time_pref, work_rhythm
- chronic_conditions (JSON array)
- medications (JSON array)
- notes

### goals

Tracks long-lived goals such as slow fat loss and target body weight. Goals are separated from profile because they may change over time.

### conditions (deprecated)

Chronic conditions now live in `profiles.chronic_conditions` as a JSON array. The separate `conditions` table is no longer used for new data.

### daily_logs

One row per date. This is the daily anchor used by all per-day tables.

It stores:

- date
- weekday
- created_at
- updated_at

It should not store calories, water total, or other values that can be recalculated.

### body_measurements

Daily body readings. For now this is usually one morning record per day.

Fields:

- weight_kg
- body_fat_pct
- bmi
- target_weight_kg
- measured_at

### meals

One row per meal slot.

Current meal slots:

- breakfast
- lunch
- snack
- dinner

Fields:

- meal_type
- meal_time
- status: pending, planned, completed, skipped
- notes

### meal_items

One row per food item in a meal.

Fields:

- name
- amount_text
- calories
- protein_g
- carbs_g
- fat_g

Macro fields are nullable because current data does not always include item-level macros.

### daily_nutrition_targets

Daily target settings.

This keeps target calories separate from actual intake. The current default is 1600 kcal.

### daily_nutrition_estimates

Stores daily macro estimates when the source only has daily totals instead of item-level macros.

Current legacy JSON has `nutrition_summary.protein_g`, `carbs_g`, and `fat_g`, but individual `meal_items` usually do not. This table preserves those values during migration. If future meal items have item-level macros, summary views can prefer summed item-level values.

### water_events

One row per water entry. Current JSON only has daily totals, but the future workflow needs multiple entries per day.

For existing data, historical totals can be represented as a single event per day with source `legacy_daily_total`.

### medications

Medication catalog (for daily tracking events). Long-term medication list lives in `profiles.medications` as JSON array.

Current medication:

- Entecavir, display name `恩替卡韦（润众）`

### medication_events

One row per scheduled/taken dose.

Fields:

- scheduled_time
- taken_time
- taken
- empty_stomach
- notes

### exercise_sessions

One row per exercise session or planned daily exercise group.

Fields:

- type
- category
- duration_min
- rpe
- status
- notes

### exercise_items

One row per exercise item inside a session.

Examples:

- 俯卧撑
- 哑铃飞鸟
- 户外快走
- 提肛运动

### health_markers

Daily low-frequency health markers that do not yet deserve separate event tables.

Fields:

- sleep_bedtime
- sleep_wake_time
- sleep_hours
- kegel_sets
- kegel_reps_per_set
- kegel_done
- kegel_notes
- prostate_symptoms
- energy_level

If sleep or symptom tracking becomes more detailed later, those can be promoted to event tables.

### daily_notes

One row per note. This replaces the free-form `notes` array in each daily JSON.

## Dashboard Export Shape

The initial SQLite migration should still export the current daily JSON shape:

```text
data/daily/YYYY-MM-DD.json
```

The exported JSON can continue to include calculated summaries:

- nutrition_summary.total_calories
- nutrition_summary.remaining_calories
- nutrition_summary.water_ml
- health_markers.water_remaining_ml

Those values should be generated from the database, not manually stored as primary facts.

## Accuracy Rules

- `total_calories` = sum of completed/planned meal item calories, depending on dashboard mode.
- `remaining_calories` = target calories - total calories.
- `water_intake_ml` = sum of water events for the date.
- `water_remaining_ml` = max(water target - water intake, 0).
- medication `taken` should be true only when `taken_time` is present or explicitly confirmed.
- daily body measurement should allow only one `morning` record per day unless a later need appears.

## Phase Plan

1. Add schema and create `health.db`. Done.
2. Add a migration script from existing daily JSON to SQLite. Done: `scripts/migrate_json_to_db.py`.
3. Add an export script from SQLite back to daily JSON. Done: `scripts/export_dashboard_data.py`.
4. Add a data-only dashboard refresher that preserves manual HTML/CSS. Done: `scripts/refresh_dashboard_data.py`.
5. Add a DB event writer for common daily records. Done: `scripts/record_health_event.py`.
6. Later, broaden DB write support for more health marker types.

## Current Commands

Create or refresh the SQLite database from legacy JSON:

```bash
python3 scripts/migrate_json_to_db.py
```

Export dashboard-compatible daily JSON from SQLite:

```bash
python3 scripts/export_dashboard_data.py
```

Rebuild the static dashboard:

```bash
python3 scripts/refresh_dashboard_data.py
```

Normal post-migration refresh:

```bash
python3 scripts/export_dashboard_data.py && python3 scripts/refresh_dashboard_data.py
```

`node build.js` is now a full template rebuild tool only. It rewrites the whole HTML file and can overwrite manual dashboard HTML/CSS changes.
