"""Content sub-apps mounted alongside the webhook FastAPI server.

Currently hosts :mod:`guide_site` — public HTML guide to the bot's
commands, rendered from Markdown sources on disk. Additional sub-apps
(stats dashboards, marriage anniversaries, etc.) can land here as
self-contained FastAPI ``APIRouter``s without touching the webhook
delivery path.
"""
