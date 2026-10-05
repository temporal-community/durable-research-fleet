"""The ONE list of what every Worker registers.

worker_local.py, worker_cloudrun.py and worker_agentcore.py each used to spell this out.
Three copies is how a Worker ends up missing an Activity on only one platform, which then
fails with "Activity function not registered" at runtime and nowhere else.

  HelloWorkflow     the infrastructure smoke test; needs no model key.
  ResearchWorkflow  the real application.
"""

from activities import say_hello
from research_activities import RESEARCH_ACTIVITIES
from research_workflow import ResearchWorkflow
from workflows import HelloWorkflow

WORKFLOWS = [HelloWorkflow, ResearchWorkflow]
ACTIVITIES = [say_hello, *RESEARCH_ACTIVITIES]
