ARG KOKORO_BASE=ghcr.io/remsky/kokoro-fastapi-gpu@sha256:79d81c41d8883611291c5785e47db2ba00765e9b89dd59de05d9f28e1945f905
FROM ${KOKORO_BASE}
USER root
COPY apply_patch.py performance_patch.py /opt/epub-kokoro-patch/
ARG ENABLE_PERFORMANCE=0
RUN if [ "$ENABLE_PERFORMANCE" = "1" ]; then \
      /app/.venv/bin/python /opt/epub-kokoro-patch/apply_patch.py --performance; \
    else \
      /app/.venv/bin/python /opt/epub-kokoro-patch/apply_patch.py; \
    fi
USER appuser
