---
paths:
  - "migrations/**/*.py"
  - "core/models.py"
---
# Migrations
- Every schema change is a new Alembic revision. Never edit an applied revision.
- Every migration must define a working `downgrade()`.
- Destructive operations (DROP, ALTER TYPE, NOT NULL on a populated column) must
  be called out explicitly in the plan before you write them.
- Never write a data migration that modifies rows in `entries`.