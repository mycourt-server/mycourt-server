FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MYCOURT_MEMBER_DB=/var/lib/mycourt/member.sqlite

RUN python -m pip install --no-cache-dir gunicorn==27.0.1 \
    && groupadd --gid 10001 mycourt \
    && useradd --uid 10001 --gid mycourt --no-create-home mycourt \
    && install -d -m 0700 -o mycourt -g mycourt /var/lib/mycourt

WORKDIR /srv/mycourt
COPY build_rankings.py member_ledger.py review_service.py ./
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--workers=2", "--threads=2", "--worker-tmp-dir=/tmp", "review_service:make_application()"]
