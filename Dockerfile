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
        libvirt-clients \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-container.txt /tmp/requirements-container.txt
COPY container/ansible-requirements.yml /tmp/ansible-requirements.yml
COPY container/apply_collection_patches.py /tmp/apply_collection_patches.py

RUN pip install -r /tmp/requirements-container.txt \
    && mkdir -p /opt/ansible/collections \
    && ansible-galaxy collection install -r /tmp/ansible-requirements.yml -p /opt/ansible/collections \
    && python /tmp/apply_collection_patches.py /opt/ansible/collections

COPY . /opt/sap-wizard

RUN chmod +x /opt/sap-wizard/container/entrypoint.sh \
    && mkdir -p /var/lib/sap-wizard/generated/runs

EXPOSE 5000

ENTRYPOINT ["/opt/sap-wizard/container/entrypoint.sh"]
