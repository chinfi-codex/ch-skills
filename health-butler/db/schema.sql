PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  age INTEGER,
  gender TEXT,
  height_cm REAL,
  bmr INTEGER,
  diet_preference TEXT,
  exercise_level TEXT,
  sleep_bedtime_pref TEXT,
  sleep_wake_time_pref TEXT,
  work_rhythm TEXT,
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
  type TEXT,
  category TEXT,
  start_time TEXT,
  duration_min INTEGER NOT NULL DEFAULT 0 CHECK (duration_min >= 0),
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
  COALESCE(SUM(we.amount_ml), 0) AS water_intake_ml,
  MAX(COALESCE(dnt.water_target_ml, 2000) - COALESCE(SUM(we.amount_ml), 0), 0) AS water_remaining_ml
FROM daily_logs dl
LEFT JOIN daily_nutrition_targets dnt ON dnt.daily_log_id = dl.id
LEFT JOIN water_events we ON we.daily_log_id = dl.id
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
