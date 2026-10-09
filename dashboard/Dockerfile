# Nutriblend admin dashboard (Streamlit). Built and started by ../docker-compose.yml
# together with the API; it talks to the API over the internal Docker network.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py ./
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit
# Host files may be owner-only (e.g. 600); make code readable by appuser.
RUN chmod -R a+rX /srv

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8501
# Listen on all interfaces INSIDE the container; docker-compose publishes the
# port on 127.0.0.1 only, so it is still not reachable from outside the machine.
CMD ["streamlit", "run", "main.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
