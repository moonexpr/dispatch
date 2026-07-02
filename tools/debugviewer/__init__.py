"""tools.debugviewer — a realtime debug viewer for dispatch workflows.

Serves an SVG statechart of every workflow YAML under ``app/workflows/`` with a
switcher to flip between them. Run it with::

    python3 -m tools.debugviewer            # http://127.0.0.1:8787
    python3 -m tools.debugviewer --port 9000 --open

The live-session overlay (watching a tick execute in realtime) is stubbed for
now — see ``server.py``'s ``/api/sessions`` endpoints.
"""
