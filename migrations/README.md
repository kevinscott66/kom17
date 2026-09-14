# Alembic migrations — multi-DB layout

The bot owns five independent SQLite files. Each has its own version
lineage, so "head" means five different revisions; pick the target with
`-x db=<name>`.

```
migrations/
├── env.py                    # dispatcher (selects the DB from -x db=…)
├── script.py.mako            # shared template for new revisions
└── versions/
    ├── users/                # 12 revisions
    ├── economy/              # 16
    ├── activity/             # 1
    ├── moderation/           # 11
    └── message_stats/        # 2
```

## Always go through the wrapper

```bash
python -m scripts.alembic_run -x db=users upgrade head
```

Not bare `alembic`. The wrapper points `version_locations` at
`migrations/versions/<db>/` *before* the `ScriptDirectory` is built.
Without it Alembic builds the tree around the default
`versions/` directory, which holds only the five sub-directories and no
`.py` files at all — so the script tree is empty, `heads` / `current` /
`history` report nothing, and `upgrade` silently succeeds having done
no work. A no-op that looks like a success is the worst failure mode a
migration tool has, so the wrapper is the only supported entry point.

## Baselining an existing database

The five baseline revisions are **no-ops**: the databases predate
Alembic, so the baselines describe a schema that is already there. An
existing deployment is brought into the lineage without touching its
data:

```bash
for db in users economy activity moderation message_stats; do
  python -m scripts.alembic_run -x db=$db stamp head
done
```

A fresh database runs them for real instead.

## Applying

```bash
for db in users economy activity moderation message_stats; do
  python -m scripts.alembic_run -x db=$db upgrade head
done

python -m scripts.alembic_run -x db=users current
python -m scripts.alembic_run -x db=users history
```

`python -m scripts.migration_status` prints the current and the head
revision of all five at once.

## Adding a revision

```bash
# autogenerate against the ORM models:
python -m scripts.alembic_run -x db=users revision --autogenerate -m "add user.referred_by"

# or write one by hand:
python -m scripts.alembic_run -x db=users revision -m "drop column foo"
```

Autogenerate only sees the metadata that belongs to the database you
name: each model module is bound to one declarative base, and `env.py`
hands Alembic just that one. A model attached to the wrong base is
invisible to its own migration.

## Notes

- The `alembic_version` table lives **inside each SQLite file**, not
  shared — so `current` for one DB never reflects another.
- `render_as_batch=True` in `env.py` — SQLite needs it for any
  `ALTER TABLE` that drops/renames columns.
