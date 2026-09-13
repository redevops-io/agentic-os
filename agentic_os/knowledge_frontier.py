"""Re-export shim — the Knowledge Frontier primitive now lives in the shared, domain-neutral
`knowledge-frontier` package, so the apps stack and learnerbot.ai share ONE engine
(https://github.com/redevops-io/knowledge-frontier).

`import agentic_os.knowledge_frontier` stays valid — the code is upstream. The apps-stack-specific
adapter (`from_frontier_choice`) lives in `agentic_os.priority_engine`, not here.
"""
from knowledge_frontier import *  # noqa: F401,F403
from knowledge_frontier import __all__  # noqa: F401
