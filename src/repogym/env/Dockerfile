# One slim image per repository: test dependencies only, never the repo's code.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG APT_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ${APT_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*
RUN git config --system safe.directory '*' \
    && git config --system protocol.file.allow always \
    && git config --system advice.detachedHead false
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --only-binary :all: --no-warn-script-location -r /requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=99.0.0
WORKDIR /work
