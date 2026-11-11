# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAP_WIZARD_STATE_DIR=/var/lib/sap-wizard \
    ANSIBLE_COLLECTIONS_PATH=/opt/ansible/collections:/usr/share/ansible/collections \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

WORKDIR /opt/sap-wizard

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-container.txt /tmp/requirements-container.txt
COPY container/ansible-requirements.yml /tmp/ansible-requirements.yml
COPY container/apply_collection_patches.py /tmp/apply_collection_patches.py

# community.sap_infrastructure from the build context 'sap_infrastructure' (see compose.yml).
# Bind mount instead of COPY: the source (incl. .git metadata) is not stored in any image layer.
RUN --mount=type=bind,from=sap_infrastructure,target=/mnt/sap_infrastructure,rw \
    pip install -r /tmp/requirements-container.txt \
    && mkdir -p /opt/ansible/collections \
    && ansible-galaxy collection install -r /tmp/ansible-requirements.yml -p /opt/ansible/collections \
    && cp -a /mnt/sap_infrastructure /tmp/community.sap_infrastructure \
    && rm -rf /tmp/community.sap_infrastructure/.git \
    && ansible-galaxy collection install /tmp/community.sap_infrastructure -p /opt/ansible/collections --force \
    && rm -rf /tmp/community.sap_infrastructure \
    && pip install -r /opt/ansible/collections/ansible_collections/azure/azcollection/requirements.txt \
    && python /tmp/apply_collection_patches.py /opt/ansible/collections

COPY . /opt/sap-wizard

RUN chmod +x /opt/sap-wizard/container/entrypoint.sh \
    && mkdir -p /var/lib/sap-wizard/generated/runs

EXPOSE 5000

ENTRYPOINT ["/opt/sap-wizard/container/entrypoint.sh"]
