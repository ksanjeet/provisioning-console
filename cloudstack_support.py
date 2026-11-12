"""Apache CloudStack support for the provisioning console.

Uses the 'cs' Python library (also required by the ngine_io.cloudstack Ansible Collection).
Credentials come from the saved provider profile, the generated job variables or,
as fallback, the CLOUDSTACK_* environment variables / cloudstack.ini file.
"""

import configparser
import json
import os
import re
from pathlib import Path
from urllib import request as urllib_request

PLATFORM = "cloudstack_vm"
ACCENT = "cloudstack"

PROFILE_FIELDS = ["cloudstack_api_url", "cloudstack_api_key", "cloudstack_api_secret", "cloudstack_zone", "owner_tag"]
PROFILE_REQUIRED_FIELDS = ["cloudstack_api_url", "cloudstack_api_key", "cloudstack_api_secret"]
DEPLOYMENT_FIELDS = [
    "cloudstack_zone",
    "cloudstack_network_name",
    "cloudstack_template_name",
    "cloudstack_hypervisor",
    "cloudstack_ha_fencing_method",
    "cloudstack_kvm_host_ssh_user",
    "cloudstack_kvm_host_ssh_private_key_file",
]
FENCING_METHODS = ["sbd_devices", "sbd_linstor_kvm", "none"]


def parse_sbd_devices(value):
    """Comma or whitespace separated device paths to list."""
    if isinstance(value, list):
        return [item for item in value if item]
    return [item for item in re.split(r"[\s,]+", value or "") if item]
REQUIRED_DEPLOYMENT_FIELDS = {
    "cloudstack_zone": "CloudStack zone",
    "cloudstack_network_name": "CloudStack guest network",
    "cloudstack_template_name": "CloudStack template",
    "dns_root_domain": "DNS root domain",
}
OS_IMAGE_KEY = "webui_selected"


def _ini_credentials():
    candidates = [
        os.environ.get("CLOUDSTACK_CONFIG", ""),
        str(Path.home() / ".cloudstack.ini"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            parser = configparser.ConfigParser()
            parser.read(candidate)
            if parser.has_section("cloudstack"):
                section = parser["cloudstack"]
                return {
                    "cloudstack_api_url": section.get("endpoint", ""),
                    "cloudstack_api_key": section.get("key", ""),
                    "cloudstack_api_secret": section.get("secret", ""),
                }
    return {}


def resolve_credentials(profile=None):
    """Return API credentials from the profile, environment or cloudstack.ini (first complete source wins)."""
    profile = profile or {}
    sources = [
        {key: profile.get(key, "") for key in PROFILE_REQUIRED_FIELDS},
        {
            "cloudstack_api_url": os.environ.get("CLOUDSTACK_ENDPOINT", ""),
            "cloudstack_api_key": os.environ.get("CLOUDSTACK_KEY", ""),
            "cloudstack_api_secret": os.environ.get("CLOUDSTACK_SECRET", ""),
        },
        _ini_credentials(),
    ]
    for source in sources:
        if all(source.get(key) and source.get(key) != "***redacted***" for key in PROFILE_REQUIRED_FIELDS):
            return source
    return {}


def credentials_from_job_vars(job_vars):
    return resolve_credentials({
        "cloudstack_api_url": job_vars.get("sap_vm_provision_cloudstack_api_url", ""),
        "cloudstack_api_key": job_vars.get("sap_vm_provision_cloudstack_api_key", ""),
        "cloudstack_api_secret": job_vars.get("sap_vm_provision_cloudstack_api_secret", ""),
    })


def build_client(credentials, timeout=30):
    try:
        from cs import CloudStack
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("The Python library 'cs' is not installed in the application environment.") from exc
    if not credentials:
        raise RuntimeError("Apache CloudStack API endpoint, key and secret are not configured.")
    return CloudStack(
        endpoint=credentials["cloudstack_api_url"],
        key=credentials["cloudstack_api_key"],
        secret=credentials["cloudstack_api_secret"],
        timeout=timeout,
        method="post",
    )


def _list(client, command, **params):
    return getattr(client, command)(fetch_list=True, **params) or []


PROVISIONING_TAG = "sap_vm_provision"


def _is_provisioned_by_console(vm):
    return any(tag.get("key") == PROVISIONING_TAG for tag in vm.get("tags", []) or [])


def _vm_summary(vm):
    nics = vm.get("nic", [])
    ips = [nic.get("ipaddress") for nic in nics if nic.get("ipaddress")]
    secondary = [ip.get("ipaddress") for nic in nics for ip in nic.get("secondaryip", []) if ip.get("ipaddress")]
    return {
        "name": vm.get("name"),
        "instance_id": vm.get("id"),
        "instance_name": vm.get("instancename"),
        "state": (vm.get("state") or "").lower(),
        "private_ip": ips[0] if ips else None,
        "secondary_ips": secondary,
        "kvm_host": vm.get("hostname"),
        "service_offering": vm.get("serviceofferingname"),
        "template": vm.get("templatename"),
    }


# Provider dashboard

def inspect_dashboard(build_provider_status, build_dashboard_resource, saved_credentials):
    profile = saved_credentials.get(PLATFORM, {})
    credentials = resolve_credentials(profile)
    if not credentials:
        return build_provider_status(
            PLATFORM,
            ACCENT,
            "not_configured",
            "Apache CloudStack API credentials are not configured.",
            ["Save the API URL, key and secret in the CloudStack provider profile."],
        )
    try:
        client = build_client(credentials, timeout=15)
        params = {"listall": True}
        zone_name = profile.get("cloudstack_zone")
        if zone_name:
            zones = _list(client, "listZones", name=zone_name)
            if zones:
                params["zoneid"] = zones[0]["id"]
        vms = _list(client, "listVirtualMachines", **params)
    except Exception as exc:
        return build_provider_status(PLATFORM, ACCENT, "error", "Apache CloudStack connectivity failed.", [str(exc)])

    resources = []
    for vm in vms:
        summary = _vm_summary(vm)
        managed = _is_provisioned_by_console(vm)
        extra = {"metadata": [
            f"Template: {summary['template'] or 'n/a'}",
            "Provisioned by Ansible Playbooks for SAP" if managed else "Not provisioned by this console (read-only)",
        ]}
        # Visibility and delete permission are decided by the console's Owner tag filter
        extra.update({"delete_payload": {"instance_id": summary["instance_id"], "name": summary["name"]},
                      "tags": {tag.get("key"): tag.get("value") for tag in vm.get("tags", []) or []}})
        resources.append(build_dashboard_resource(
            PLATFORM,
            ACCENT,
            summary["instance_id"],
            summary["name"],
            summary["state"],
            f"{summary['private_ip'] or 'no IP'} on {summary['kvm_host'] or 'unassigned host'} ({summary['service_offering'] or 'n/a'})",
            **extra,
        ))
    details = [f"API endpoint: {credentials['cloudstack_api_url']}"]
    if profile.get("cloudstack_zone"):
        details.append(f"Zone: {profile['cloudstack_zone']}")
    return build_provider_status(
        PLATFORM,
        ACCENT,
        "connected",
        f"Connected to Apache CloudStack. {len(resources)} instance(s) visible.",
        details,
        sorted(resources, key=lambda item: (item["name"] or "").lower()),
    )


def delete_dashboard_resource(payload, saved_credentials):
    """Destroy and expunge one VM provisioned by the console, including its data volumes."""
    instance_id = payload.get("instance_id")
    if not instance_id:
        raise RuntimeError("Missing CloudStack instance ID.")
    client = build_client(resolve_credentials(saved_credentials.get(PLATFORM, {})), timeout=600)
    vms = _list(client, "listVirtualMachines", id=instance_id, listall=True)
    if not vms:
        raise RuntimeError(f"CloudStack instance {instance_id} was not found.")
    vm = vms[0]
    owner = saved_credentials.get(PLATFORM, {}).get("owner_tag", "")
    tags = {tag.get("key"): tag.get("value") for tag in vm.get("tags", []) or []}
    if not owner or not any(str(k).lower() == "owner" and str(v).strip().lower() == owner.strip().lower() for k, v in tags.items()):
        raise RuntimeError(f"CloudStack instance '{vm.get('name')}' is not tagged with your Owner tag and is not deleted.")
    data_volumes = [vol["id"] for vol in _list(client, "listVolumes", virtualmachineid=instance_id, type="DATADISK", listall=True)]
    params = {"id": instance_id, "expunge": True, "fetch_result": True}
    if data_volumes:
        params["volumeids"] = ",".join(data_volumes)
    client.destroyVirtualMachine(**params)
    message = f"Destroyed and expunged CloudStack instance '{vm.get('name')}' with {len(data_volumes)} data volume(s)."
    deleted_sbd = _delete_orphaned_linstor_sbd(client, vm.get("name"))
    if deleted_sbd:
        message += f" Deleted SBD shared disk(s) of the HA cluster: {', '.join(deleted_sbd)}."
    return {"message": message}


def _delete_orphaned_linstor_sbd(client, deleted_vm_name):
    """Delete LINSTOR SBD disks (fencing method sbd_linstor_kvm) once no VM of their HA cluster exists anymore.

    A reused SBD disk keeps old fencing messages (e.g. 'reset'), which block SBD start-up on a new cluster
    with the same host names (SBD_STARTMODE=clean).
    """
    try:  # best effort: the VM is already destroyed, SBD cleanup must not fail the delete
        linstor_url = _linstor_url(client, {})
    except Exception:
        return []
    if not linstor_url:
        return []
    try:
        with urllib_request.urlopen(f"{linstor_url.rstrip('/')}/v1/resource-definitions", timeout=15) as response:
            definitions = json.loads(response.read().decode("utf-8") or "[]")
    except Exception:
        return []
    deleted = []
    for definition in definitions:
        owners = [host for host in (definition.get("props") or {}).get("Aux/sap_vm_provision", "").split(",") if host]
        if not owners or deleted_vm_name not in owners:
            continue
        remaining = [host for host in owners if host != deleted_vm_name
                     and any(vm.get("name") == host for vm in _list(client, "listVirtualMachines", name=host, listall=True))]
        if remaining:
            continue
        try:
            req = urllib_request.Request(f"{linstor_url.rstrip('/')}/v1/resource-definitions/{definition['name']}", method="DELETE")
            urllib_request.urlopen(req, timeout=60).read()
            deleted.append(definition["name"])
        except Exception:
            continue
    return deleted


# Preflight

def run_preflight_check(config, build_check):
    checks = []
    credentials = resolve_credentials(config)
    if not credentials:
        return [build_check("CloudStack API credentials", False, "API URL, key and secret are not configured.")]
    try:
        client = build_client(credentials, timeout=20)
        capabilities = client.listCapabilities().get("capability", {})
        checks.append(build_check(
            "CloudStack API connectivity", True, f"Connected to CloudStack {capabilities.get('cloudstackversion', '')}".strip()))
    except Exception as exc:
        return [build_check("CloudStack API connectivity", False, str(exc))]

    zone_id = None
    zone_name = config.get("cloudstack_zone")
    zones = _list(client, "listZones", name=zone_name) if zone_name else []
    zones = [zone for zone in zones if zone.get("name") == zone_name]
    if zones:
        zone_id = zones[0]["id"]
        checks.append(build_check("CloudStack zone", True, f"Zone '{zone_name}' ({zones[0].get('networktype')})."))
    else:
        checks.append(build_check("CloudStack zone", False, f"Zone '{zone_name}' was not found."))

    network_name = config.get("cloudstack_network_name")
    networks = [net for net in _list(client, "listNetworks", listall=True, **({"zoneid": zone_id} if zone_id else {}))
                if net.get("name") == network_name]
    checks.append(build_check(
        "CloudStack guest network",
        bool(networks),
        f"Network '{network_name}' ({networks[0].get('type')}, {networks[0].get('cidr')})." if networks
        else f"Network '{network_name}' was not found.",
    ))

    template_name = config.get("cloudstack_template_name")
    templates = [tpl for tpl in _list(client, "listTemplates", templatefilter="executable", **({"zoneid": zone_id} if zone_id else {}))
                 if tpl.get("name") == template_name]
    ready = [tpl for tpl in templates if tpl.get("isready")]
    checks.append(build_check(
        "CloudStack template",
        bool(ready),
        f"Template '{template_name}' is ready." if ready
        else f"Template '{template_name}' was not found or is not ready.",
    ))

    offerings = {item.get("name") for item in _list(client, "listServiceOfferings")}
    requested = sorted({
        host.get("cloudstack_vm_service_offering")
        for host in (config.get("_cloudstack_selected_hosts") or {}).values()
        if isinstance(host, dict) and host.get("cloudstack_vm_service_offering")
    })
    missing = [name for name in requested if name not in offerings]
    placeholders = [name for name in requested if name.startswith("ENTER_")]
    if placeholders:
        checks.append(build_check(
            "CloudStack service offerings",
            False,
            "The host plan still contains placeholder service offering(s); set 'cloudstack_vm_service_offering' "
            "for each host in the YAML override (sap_vm_provision_cloudstack_vm_host_specifications_dictionary).",
        ))
        return checks
    if requested:
        checks.append(build_check(
            "CloudStack service offerings",
            not missing,
            f"Missing service offering(s): {', '.join(missing)}." if missing else f"Available: {', '.join(requested)}.",
        ))
    return checks


# Job resources

def _job_hostnames(job_vars):
    host_dict = job_vars.get("sap_vm_provision_cloudstack_vm_host_specifications_dictionary", {})
    plan = job_vars.get("sap_vm_provision_host_specification_plan")
    selected = host_dict.get(plan, {}) if isinstance(host_dict, dict) and plan else {}
    return sorted(selected.keys()) if isinstance(selected, dict) else []


def _linstor_sbd_resources(job_vars, hostnames, linstor_url):
    if not linstor_url or not hostnames:
        return []
    try:
        with urllib_request.urlopen(f"{linstor_url.rstrip('/')}/v1/resource-definitions", timeout=15) as response:
            definitions = json.loads(response.read().decode("utf-8") or "[]")
    except Exception:
        return []
    prefix = job_vars.get("sap_vm_provision_cloudstack_ha_sbd_linstor_resource_prefix", "sbd")
    result = []
    for definition in definitions:
        owner_hosts = (definition.get("props") or {}).get("Aux/sap_vm_provision", "")
        if definition.get("name", "").startswith(f"{prefix}-") and owner_hosts and set(owner_hosts.split(",")) <= set(hostnames):
            result.append(definition["name"])
    return result


def _linstor_url(client, job_vars):
    url = job_vars.get("sap_vm_provision_cloudstack_ha_sbd_linstor_api_url")
    if url:
        return url
    for pool in _list(client, "listStoragePools"):
        if pool.get("type") == "Linstor" and pool.get("ipaddress"):
            return pool["ipaddress"]
    return ""


def list_job_resources(job_vars, platform_label):
    hostnames = _job_hostnames(job_vars)
    if not hostnames:
        return {"platform": PLATFORM, "instances": [], "volumes": [], "message": "No CloudStack hostnames were found in the generated job variables."}
    client = build_client(credentials_from_job_vars(job_vars))
    instances = []
    volumes = []
    for hostname in hostnames:
        for vm in _list(client, "listVirtualMachines", name=hostname, listall=True):
            # Same name is not enough: only VMs created by sap_vm_provision are part of the job
            if vm.get("name") != hostname or not _is_provisioned_by_console(vm):
                continue
            instances.append(_vm_summary(vm))
            for volume in _list(client, "listVolumes", virtualmachineid=vm["id"], listall=True):
                volumes.append({
                    "volume_id": volume.get("id"),
                    "name": volume.get("name"),
                    "device_name": f"deviceid {volume.get('deviceid')}",
                    "size_gib": int((volume.get("size") or 0) / 1024 ** 3),
                    "state": volume.get("state"),
                    "volume_type": volume.get("type"),
                    "attached_instance_id": vm["id"],
                    "attached_instance_name": hostname,
                    "delete_on_termination": volume.get("type") == "ROOT",
                })
    linstor_url = _linstor_url(client, job_vars)
    for resource in _linstor_sbd_resources(job_vars, hostnames, linstor_url):
        volumes.append({
            "volume_id": f"linstor:{resource}",
            "name": resource,
            "device_name": "SBD shared disk (LINSTOR, attached with virsh, fencing method sbd_linstor_kvm)",
            "state": "external",
            "volume_type": "SBD",
            "delete_on_termination": False,
        })
    return {
        "platform": PLATFORM,
        "platform_label": platform_label,
        "instances": sorted(instances, key=lambda item: item["name"]),
        "volumes": volumes,
        "message": f"Found {len(instances)} instance(s) for this job.",
    }


def terminate_job_resources(job_vars, platform_label):
    resources = list_job_resources(job_vars, platform_label)
    instances = resources.get("instances", [])
    if not instances:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No CloudStack instances were found for this job."}
    client = build_client(credentials_from_job_vars(job_vars), timeout=600)

    data_volumes = [item for item in resources.get("volumes", []) if item.get("volume_type") == "DATADISK"]
    terminated = []
    deleted = []
    for instance in instances:
        volume_ids = [vol["volume_id"] for vol in data_volumes if vol.get("attached_instance_id") == instance["instance_id"]]
        params = {"id": instance["instance_id"], "expunge": True, "fetch_result": True}
        if volume_ids:
            params["volumeids"] = ",".join(volume_ids)
        client.destroyVirtualMachine(**params)
        terminated.append(instance["name"])
        deleted.extend(vol["name"] for vol in data_volumes if vol["volume_id"] in volume_ids)

    linstor_url = _linstor_url(client, job_vars)
    for volume in resources.get("volumes", []):
        if volume.get("volume_type") != "SBD" or not linstor_url:
            continue
        try:
            req = urllib_request.Request(
                f"{linstor_url.rstrip('/')}/v1/resource-definitions/{volume['name']}", method="DELETE")
            urllib_request.urlopen(req, timeout=60).read()
            deleted.append(volume["name"])
        except Exception:
            continue

    return {
        "terminated_instances": terminated,
        "deleted_volumes": deleted,
        "message": f"Destroyed and expunged {len(terminated)} instance(s). Deleted {len(deleted)} data volume(s) / SBD disk(s).",
    }
