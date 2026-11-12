"""``python -m document_agent.skills.admin`` 入口。"""

import sys

from .cli import run

raise SystemExit(run(sys.argv[1:], prog="document_agent.skills.admin"))
