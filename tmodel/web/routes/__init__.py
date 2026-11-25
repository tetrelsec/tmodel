"""
Route handlers, grouped by what they work on. Each is a plain Starlette
endpoint that starts with `c = ctx(request)`; the URL table (and its order:
specific paths before /m/{mid}/{coll}) is in one place, `app.create_app`.

  models.py    model list: create, import, zip of all models; the JSON API
  entities.py  a collection's table, entity forms, delete, inline <tr> rows, side panel
  pages.py     yaml, settings, copy, report, diagram, search (go)
  exports.py   YAML/JSON export, CSV of a table or of the threat register
  history.py   git history: one commit's diff, restore
"""
