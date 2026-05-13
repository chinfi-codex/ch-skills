PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  age INTEGER,
  gender TEXT,
  height_cm REAL,
  weight_kg REAL,
  target_weight_kg REAL,
  bmi REAL,
  bmr INTEGER,
  diet_preference TEXT,
  exercise_level TEXT,
  sleep_bedtime_pref TEXT,
  sleep_wake_time_pref TEXT,
  work_rhythm TEXT,
  chronic_conditions TEXT,  -- JSON array
  medications TEXT,           -- JSON array
  notes TEXT
);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL DEFAULT 1 REFERENCES profiles(id) ON DELETE CASCADE,
  goal_type TEXT NOT NULL,
  label TEXT NOT NULL,
  target_weight_kg REAL,
  weekly_rate_kg REAL,
  target_calories INTEGER,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conditions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL DEFAULT 1 REFERENCES profiles(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  medication_note TEXT,
  monitoring_note TEXT,
  management_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL UNIQUE,
  weekday TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_measurements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  measurement_type TEXT NOT NULL DEFAULT 'morning',
  measured_at TEXT,
  weight_kg REAL,
  body_fat_pct REAL,
  bmi REAL,
  target_weight_kg REAL,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (daily_log_id, measurement_type)
);

CREATE TABLE IF NOT EXISTS daily_nutrition_targets (
  daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
  target_calories INTEGER NOT NULL DEFAULT 1600,
  protein_g REAL,
  carbs_g REAL,
  fat_g REAL,
  water_target_ml INTEGER NOT NULL DEFAULT 2000,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_nutrition_estimates (
  daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
  protein_g REAL,
  carbs_g REAL,
  fat_g REAL,
  source TEXT NOT NULL DEFAULT 'manual_estimate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'snack', 'dinner')),
  meal_time TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'planned', 'completed', 'skipped')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (daily_log_id, meal_type)
);

CREATE TABLE IF NOT EXISTS meal_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  amount_text TEXT,
  calories REAL NOT NULL DEFAULT 0,
  protein_g REAL,
  carbs_g REAL,
  fat_g REAL,
  water_ml INTEGER,
  calorie_source TEXT NOT NULL DEFAULT 'manual_estimate',
  water_source TEXT,
  estimate_confidence TEXT NOT NULL DEFAULT 'medium',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS water_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  event_time TEXT,
  amount_ml INTEGER NOT NULL CHECK (amount_ml >= 0),
  source TEXT NOT NULL DEFAULT 'manual',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  default_scheduled_time TEXT,
  instructions TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medication_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE RESTRICT,
  scheduled_time TEXT,
  taken_time TEXT,
  taken INTEGER NOT NULL DEFAULT 0 CHECK (taken IN (0, 1)),
  empty_stomach INTEGER NOT NULL DEFAULT 0 CHECK (empty_stomach IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (daily_log_id, medication_id, scheduled_time)
);

CREATE TABLE IF NOT EXISTS exercise_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  planned_instance_id INTEGER REFERENCES daily_plan_instances(id) ON DELETE SET NULL,
  type TEXT,
  category TEXT,
  start_time TEXT,
  duration_min INTEGER NOT NULL DEFAULT 0 CHECK (duration_min >= 0),
  active_energy_kcal REAL,
  burn_source TEXT NOT NULL DEFAULT 'manual_estimate',
  unit_summary TEXT,
  rpe INTEGER CHECK (rpe IS NULL OR (rpe >= 1 AND rpe <= 10)),
  status TEXT NOT NULL CHECK (status IN ('pending', 'planned', 'partial', 'completed', 'skipped')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exercise_session_id INTEGER NOT NULL REFERENCES exercise_sessions(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sets INTEGER,
  reps TEXT,
  unit_type TEXT,
  unit_value REAL,
  duration_min_estimated INTEGER,
  calories_burned REAL,
  done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_markers (
  daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
  sleep_bedtime TEXT,
  sleep_wake_time TEXT,
  sleep_hours REAL,
  kegel_sets INTEGER NOT NULL DEFAULT 0 CHECK (kegel_sets >= 0),
  kegel_reps_per_set INTEGER,
  kegel_done INTEGER NOT NULL DEFAULT 0 CHECK (kegel_done IN (0, 1)),
  kegel_notes TEXT,
  prostate_symptoms TEXT,
  energy_level INTEGER CHECK (energy_level IS NULL OR (energy_level >= 1 AND energy_level <= 10)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  note TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day_of_week INTEGER NOT NULL,
  plan_type TEXT NOT NULL,
  category TEXT,
  duration_min INTEGER,
  exercises_json TEXT,
  notes TEXT,
  active INTEGER DEFAULT 1,
  plan_version TEXT,
  profile_snapshot_json TEXT,
  goal_snapshot_json TEXT,
  valid_from TEXT,
  valid_to TEXT,
  generation_reason TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_energy_budgets (
  daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
  mode TEXT NOT NULL DEFAULT 'manual',
  bmr REAL NOT NULL,
  activity_multiplier REAL NOT NULL,
  base_burn_kcal REAL NOT NULL,
  exercise_burn_kcal REAL NOT NULL DEFAULT 0,
  target_deficit_kcal REAL NOT NULL DEFAULT 500,
  intake_limit_kcal REAL NOT NULL,
  actual_intake_kcal REAL NOT NULL DEFAULT 0,
  remaining_kcal REAL NOT NULL,
  computed_at TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS daily_plan_instances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_log_id INTEGER NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  plan_date TEXT NOT NULL UNIQUE,
  source_plan_id INTEGER REFERENCES exercise_plans(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'adjusted', 'partial', 'completed', 'skipped', 'rest')),
  planned_duration_min INTEGER NOT NULL DEFAULT 0,
  adjusted_duration_min INTEGER NOT NULL DEFAULT 0,
  adjustment_level TEXT NOT NULL DEFAULT 'none',
  adjustment_reason TEXT,
  created_by TEXT NOT NULL DEFAULT 'weekly_generator',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_plan_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  daily_plan_instance_id INTEGER NOT NULL REFERENCES daily_plan_instances(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  unit_type TEXT,
  target_value TEXT,
  sets TEXT,
  reps TEXT,
  duration_min INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'completed', 'skipped')),
  notes TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reviews (
  daily_log_id INTEGER PRIMARY KEY REFERENCES daily_logs(id) ON DELETE CASCADE,
  review_date TEXT NOT NULL UNIQUE,
  intake_status TEXT NOT NULL DEFAULT 'unknown',
  water_status TEXT NOT NULL DEFAULT 'unknown',
  exercise_status TEXT NOT NULL DEFAULT 'unknown',
  weight_signal TEXT,
  fatigue_signal TEXT,
  summary TEXT,
  next_adjustment TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

DROP VIEW IF EXISTS daily_nutrition_summary;
DROP VIEW IF EXISTS daily_water_summary;

CREATE VIEW daily_nutrition_summary AS
SELECT
  dl.id AS daily_log_id,
  dl.date,
  COALESCE(dnt.target_calories, 1600) AS target_calories,
  COALESCE(SUM(mi.calories), 0) AS total_calories,
  COALESCE(dnt.target_calories, 1600) - COALESCE(SUM(mi.calories), 0) AS remaining_calories,
  COALESCE(SUM(mi.protein_g), dne.protein_g) AS protein_g,
  COALESCE(SUM(mi.carbs_g), dne.carbs_g) AS carbs_g,
  COALESCE(SUM(mi.fat_g), dne.fat_g) AS fat_g
FROM daily_logs dl
LEFT JOIN daily_nutrition_targets dnt ON dnt.daily_log_id = dl.id
LEFT JOIN daily_nutrition_estimates dne ON dne.daily_log_id = dl.id
LEFT JOIN meals m ON m.daily_log_id = dl.id AND m.status = 'completed'
LEFT JOIN meal_items mi ON mi.meal_id = m.id
GROUP BY dl.id;

CREATE VIEW daily_water_summary AS
SELECT
  dl.id AS daily_log_id,
  dl.date,
  COALESCE(dnt.water_target_ml, 2000) AS water_target_ml,
  COALESCE(we_sum.water_events_ml, 0) + COALESCE(mi_sum.meal_water_ml, 0) AS water_intake_ml,
  MAX(COALESCE(dnt.water_target_ml, 2000) - (COALESCE(we_sum.water_events_ml, 0) + COALESCE(mi_sum.meal_water_ml, 0)), 0) AS water_remaining_ml
FROM daily_logs dl
LEFT JOIN daily_nutrition_targets dnt ON dnt.daily_log_id = dl.id
LEFT JOIN (
  SELECT daily_log_id, SUM(amount_ml) AS water_events_ml
  FROM water_events
  GROUP BY daily_log_id
) we_sum ON we_sum.daily_log_id = dl.id
LEFT JOIN (
  SELECT m.daily_log_id, SUM(COALESCE(mi.water_ml, 0)) AS meal_water_ml
  FROM meals m
  JOIN meal_items mi ON mi.meal_id = m.id
  WHERE m.status = 'completed'
  GROUP BY m.daily_log_id
) mi_sum ON mi_sum.daily_log_id = dl.id
GROUP BY dl.id;

CREATE INDEX IF NOT EXISTS idx_body_measurements_daily_log_id ON body_measurements(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_daily_nutrition_estimates_daily_log_id ON daily_nutrition_estimates(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_meals_daily_log_id ON meals(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_meal_items_meal_id ON meal_items(meal_id);
CREATE INDEX IF NOT EXISTS idx_water_events_daily_log_id ON water_events(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_medication_events_daily_log_id ON medication_events(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_exercise_sessions_daily_log_id ON exercise_sessions(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_exercise_items_session_id ON exercise_items(exercise_session_id);
CREATE INDEX IF NOT EXISTS idx_daily_notes_daily_log_id ON daily_notes(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_daily_plan_instances_daily_log_id ON daily_plan_instances(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_daily_plan_items_instance_id ON daily_plan_items(daily_plan_instance_id);
CREATE INDEX IF NOT EXISTS idx_daily_reviews_review_date ON daily_reviews(review_date);
