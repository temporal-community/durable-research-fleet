FROM python:3.12-slim

# Don't buffer stdout/stderr — otherwise the Worker's startup and shutdown logs
# can be lost when Cloud Run stops the instance during scale-in.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Flat layout: runtime + app are all siblings, so there's no package path to
# wire up. runtime.py is the infra half and MUST be here — worker_cloudrun.py
# imports it, so omitting it makes the image crash on start.
#
#   runtime.py                  the app-agnostic Serverless Worker runtime
#   workflows/activities.py     the hello app — the infrastructure smoke test
#   llm, research_*             the research app
#   web.py + web/               the request-serving tier
COPY runtime.py workflows.py activities.py worker_cloudrun.py ./
COPY llm.py research_types.py research_activities.py research_workflow.py ./
COPY web.py ./
COPY web/ ./web/

# The Learn cards the page renders in its dialog. This is the ONE .md file that belongs
# in the image — `.dockerignore` drops the rest, and `docs/`+`decisions/` wholesale.
# Without this line the Learn modal 404s on Cloud Run while working perfectly locally.
COPY learn/README.md ./learn/README.md

# Run as non-root. Nothing here needs write access to the filesystem.
RUN useradd --create-home --uid 10001 worker
USER worker

# ONE IMAGE, TWO ENTRYPOINTS.
#
# The default is the Worker, which is what the Cloud Run Worker Pool runs. The
# Cloud Run *Service* that serves the phone page and dashboard runs the same image
# and overrides this command (see terraform/web.tf):
#
#     ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
#
# Same build, same push, two deployments — and no chance of the Worker and the web
# tier drifting to different versions of the shared modules.
CMD ["python", "worker_cloudrun.py"]
