FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py planner.py executor.py samples.py ./
COPY templates/ templates/

ENV RUNS_DIR=/data/runs \
    PORT=8000
RUN mkdir -p /data/runs
VOLUME /data

EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "1", "app:app"]
