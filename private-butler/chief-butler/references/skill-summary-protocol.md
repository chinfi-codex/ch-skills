# Chief Butler Skill Summary Protocol

Each managed skill should expose a compact summary JSON at:

```text
skills/<skill-name>/data/summary.json
```

Required fields:

```json
{
  "skill": "skill-name",
  "status": "ok",
  "updated_at": "ISO-8601 timestamp",
  "cards": [],
  "alerts": [],
  "links": {}
}
```

Rules:

- The child skill remains the authority for its own facts.
- Chief Butler reads summaries and coordinates display/refresh only.
- Summary data should be small, stable, and safe to render.
- A child skill may expose a detail dashboard link, but Chief Butler owns the top-level dashboard.
