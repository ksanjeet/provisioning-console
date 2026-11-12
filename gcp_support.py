"""Google Cloud support for console jobs: resource listing and teardown (Compute Engine REST API via google-auth)."""

import re
import time

PLATFORM = "gcp_ce_vm"
COMPUTE = "https://compute.googleapis.com/compute/v1"
DNS = "https://dns.googleapis.com/dns/v1"
LB_NAME_VARS = (
    "sap_vm_provision_ha_load_balancer_name_hana",
    "sap_vm_provision_ha_load_balancer_name_anydb",
    "sap_vm_provision_ha_load_balancer_name_nwas",
)
HA_HOSTNAME_SUFFIX = "-ha"
# Load balancer names of the role defaults are shared by all deployments that keep them: never deleted by teardown
DEFAULT_LB_NAMES = {"lb-sap-ha-hana", "lb-sap-ha-anydb", "lb-sap-ha-nwas"}
OWNER_TAG_KEY = "Owner"


def label_value(value):
    """Value as stored in a Google Cloud label (same conversion as sap_vm_provision for GCP labels)."""
    converted = str(value or "").lower().replace("@", "-at-")
    return re.sub(r"[^a-z0-9_-]", "_", converted)[:63]


def build_session(credentials_path):
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("google-auth and requests are required for Google Cloud.") from exc
    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


class GcpJob:
    """Context of one console job on Google Cloud."""

    def __init__(self, job_vars, session, planned_hosts):
        self.vars = job_vars
        self.session = session
        self.project = job_vars.get("sap_vm_provision_gcp_project")
        self.zone = job_vars.get("sap_vm_provision_gcp_region_zone")
        self.region = job_vars.get("sap_vm_provision_gcp_region") or (self.zone or "")[:-2]
        if "{{" in str(self.region):
            self.region = (self.zone or "")[:-2]
        self.hosts = [host["name"] for host in planned_hosts]
        self.errors = []
        tags = job_vars.get("sap_vm_provision_resource_tags") or {}
        owner = next((value for key, value in tags.items() if str(key).lower() == OWNER_TAG_KEY.lower()), "") \
            if isinstance(tags, dict) else ""
        self.owner_label = label_value(owner) if owner else ""

    # --- REST helpers -------------------------------------------------------------------------------------------
    def get(self, url, **params):
        response = self.session.get(url, params=params or None, timeout=60)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def delete(self, url, label):
        """Delete a resource and wait for its operation. Returns True if deleted, False if missing."""
        try:
            response = self.session.delete(url, timeout=60)
            if response.status_code == 404:
                return False
            response.raise_for_status()
            self._wait(response.json())
            return True
        except Exception as exc:  # collect and continue with the remaining resources
            self.errors.append(f"{label}: {exc}")
            return False

    def _wait(self, operation, timeout=900):
        if not isinstance(operation, dict) or "selfLink" not in operation or operation.get("kind", "").startswith("dns"):
            return
        deadline = time.time() + timeout
        while operation.get("status") != "DONE" and time.time() < deadline:
            time.sleep(3)
            operation = self.session.get(operation["selfLink"], timeout=60).json()
        if operation.get("error"):
            raise RuntimeError(operation["error"])

    def project_url(self, path):
        return f"{COMPUTE}/projects/{self.project}/{path}"

    # --- discovery ----------------------------------------------------------------------------------------------
    def instances(self):
        found = []
        for name in self.hosts:
            instance = self.get(self.project_url(f"zones/{self.zone}/instances/{name}"))
            if instance:
                found.append(instance)
        return found

    def load_balancer_names(self):
        names = []
        for var in LB_NAME_VARS:
            name = self.vars.get(var)
            if not name:
                continue
            if name in DEFAULT_LB_NAMES:
                note = f"load balancer resources '{name}*' kept: default name shared by other deployments"
                if note not in self.errors:
                    self.errors.append(note)
                continue
            names.append(name)
        return names

    def owned(self, resource):
        labels = resource.get("labels") or {}
        return bool(self.owner_label) and any(
            str(key).lower() == OWNER_TAG_KEY.lower() and value == self.owner_label for key, value in labels.items())


def list_job_resources(job, platform_label):
    instances, volumes = [], []
    for instance in job.instances():
        nic = (instance.get("networkInterfaces") or [{}])[0]
        instances.append({
            "name": instance["name"],
            "instance_id": instance.get("id"),
            "state": (instance.get("status") or "").lower(),
            "private_ip": nic.get("networkIP"),
            "zone": job.zone,
            "size": instance.get("machineType", "").rsplit("/", 1)[-1],
        })
        for disk in instance.get("disks") or []:
            volumes.append({
                "volume_id": disk.get("source"),
                "name": disk.get("source", "").rsplit("/", 1)[-1],
                "device_name": disk.get("deviceName"),
                "size_gib": int(disk.get("diskSizeGb") or 0),
                "attached_instance_name": instance["name"],
                "delete_on_termination": bool(disk.get("autoDelete")),
            })
    return {
        "platform": PLATFORM,
        "platform_label": platform_label,
        "instances": instances,
        "volumes": volumes,
        "message": f"Found {len(instances)} instance(s) for this job.",
    }


def _delete_load_balancers(job, deleted):
    """Delete the job's HA load balancer resources (regional/zonal) and unused global health checks."""
    for base in job.load_balancer_names():
        for path, label in [
            (f"regions/{job.region}/forwardingRules/{base}-fwd-rule", "forwarding rule"),
        ] + [(f"regions/{job.region}/forwardingRules/{base}-rule{index}", "forwarding rule") for index in range(1, 4)] + [
            (f"regions/{job.region}/backendServices/{base}-backend-service", "backend service"),
            (f"regions/{job.region}/backendServices/{base}-ascs-backend-service", "backend service"),
            (f"regions/{job.region}/backendServices/{base}-ers-backend-service", "backend service"),
        ]:
            if job.delete(job.project_url(path), f"{label} {path.rsplit('/', 1)[-1]}"):
                deleted.append(path.rsplit("/", 1)[-1])
        for suffix in ("primary", "secondary", "ascs", "ers"):
            name = f"{base}-{suffix}-instance-group"
            if job.delete(job.project_url(f"zones/{job.zone}/instanceGroups/{name}"), f"instance group {name}"):
                deleted.append(name)
        for name in (f"{base}-reserved-static-ip-vip", f"{base}-ascs-reserved-static-ip-vip", f"{base}-ers-reserved-static-ip-vip"):
            if job.delete(job.project_url(f"regions/{job.region}/addresses/{name}"), f"address {name}"):
                deleted.append(name)

    # Global health checks: delete only if no backend service (any region) still references them
    in_use = set()
    aggregated = job.get(job.project_url("aggregated/backendServices")) or {}
    for scoped in (aggregated.get("items") or {}).values():
        for service in scoped.get("backendServices") or []:
            in_use.update(check.rsplit("/", 1)[-1] for check in service.get("healthChecks") or [])
    for base in job.load_balancer_names():
        for name in (f"{base}-probe-hc-vip", f"{base}-ascs-probe-hc-vip", f"{base}-ers-probe-hc-vip"):
            if name in in_use:
                job.errors.append(f"health check {name} kept: still used by another load balancer")
                continue
            if job.delete(job.project_url(f"global/healthChecks/{name}"), f"health check {name}"):
                deleted.append(name)


def _delete_dns_records(job, deleted, host_names):
    domain = (job.vars.get("sap_vm_provision_dns_root_domain") or "").rstrip(".")
    if not domain:
        return
    zones = (job.get(f"{DNS}/projects/{job.project}/managedZones", dnsName=f"{domain}.") or {}).get("managedZones") or []
    for zone in zones:
        for host in host_names:
            for record in (f"{host}.{domain}.", f"{host}{HA_HOSTNAME_SUFFIX}.{domain}."):
                url = f"{DNS}/projects/{job.project}/managedZones/{zone['name']}/rrsets/{record}/A"
                if job.delete(url, f"DNS record {record}"):
                    deleted.append(record)


def _delete_leftovers(job):
    """VMs of the job deleted outside the console: unattached disks <host>-vol-* with the job's Owner label and the
    job's load balancer resources (unique names per deployment)."""
    deleted_volumes, deleted = [], []
    disks = (job.get(job.project_url(f"zones/{job.zone}/disks")) or {}).get("items") or []
    for disk in disks:
        if disk.get("users") or not job.owned(disk):
            continue
        if any(disk["name"] == host or disk["name"].startswith(f"{host}-vol-") for host in job.hosts):
            if job.delete(disk["selfLink"], f"disk {disk['name']}"):
                deleted_volumes.append(disk["name"])
    _delete_load_balancers(job, deleted)
    message = (f"No Google Cloud VMs of this job were found. Deleted {len(deleted_volumes)} leftover disk(s) with your "
               f"Owner label and {len(deleted)} load balancer resource(s).")
    if job.errors:
        message += " Notes: " + "; ".join(job.errors)
    return {"terminated_instances": [], "deleted_volumes": deleted_volumes + deleted, "message": message}


def terminate_job_resources(job):
    instances = job.instances()
    if not instances:
        if job.owner_label:
            return _delete_leftovers(job)
        return {"terminated_instances": [], "deleted_volumes": [],
                "message": "No Google Cloud VMs of this job were found; shared resources were not changed."}
    deleted = []
    _delete_load_balancers(job, deleted)

    keep_disks = []
    terminated = []
    for instance in instances:
        keep_disks += [disk["source"] for disk in instance.get("disks") or [] if not disk.get("autoDelete")]
        if job.delete(job.project_url(f"zones/{job.zone}/instances/{instance['name']}"), f"instance {instance['name']}"):
            terminated.append(instance["name"])
    deleted_volumes = []
    for disk_url in keep_disks:
        disk = job.get(disk_url)
        if disk and disk.get("users"):
            job.errors.append(f"disk {disk['name']} kept: still attached")
            continue
        if disk and job.delete(disk_url, f"disk {disk['name']}"):
            deleted_volumes.append(disk["name"])

    _delete_dns_records(job, deleted, [instance["name"] for instance in instances])

    message = (f"Deleted {len(terminated)} instance(s), {len(deleted_volumes)} disk(s) and "
               f"{len(deleted)} load balancer / DNS resource(s).")
    if job.errors:
        message += " Notes: " + "; ".join(job.errors)
    return {"terminated_instances": terminated, "deleted_volumes": deleted_volumes + deleted, "message": message}
