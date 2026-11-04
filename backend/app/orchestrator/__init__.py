"""The orchestrator (Assignment 2): plain code inside the A1 backend process.

It starts runs, gives each worker its share of the budget, calls the researcher and the verifier
over HTTP only (worker_client.py), validates every answer (validate.py), and is the only component
that reads or writes run history and last run's top K (service.py, through SQLAlchemy).

It imports nothing from researcher/, verifier/ or tracker/ -- only the shared wire contract.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:      # the backend runs from backend/; shared/ lives at the repo root
    sys.path.insert(0, str(_ROOT))
