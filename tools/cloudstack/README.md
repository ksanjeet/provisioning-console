# Apache CloudStack prerequisites for SAP provisioning

The console provisions SAP systems on an existing Apache CloudStack zone. Prepare the following once per zone. Names
are examples; enter your own in the wizard or the YAML override.

## Template (OS image)

SAP HANA needs a SLES for SAP or RHEL for SAP template with cloud-init (CloudStack datasource) and Python 3.11 or later.
[sles4sap_15sp7_template/build.sh](sles4sap_15sp7_template/build.sh) builds a SLES for SAP 15 SP7 template from the
SLE 15 SP7 Full ISO with AutoYaST (qcow2 for KVM, vhd for XenServer/XCP-ng):

```bash
tools/cloudstack/sles4sap_15sp7_template/build.sh SLE-15-SP7-Full-x86_64-GM-Media1.iso /var/tmp/template 20 qcow2
```

Register the image as a template (`cloudmonkey register template` or the CloudStack UI, *Templates > Register
template*, OS type SUSE Linux Enterprise Server 15, *Password enabled* off, *Dynamically scalable* optional) and enter
its name in the wizard (`sap_vm_provision_cloudstack_vm_host_os_image_dictionary`).

If the upload is rejected because secondary storage is "full", CloudStack refuses uploads above 90 % usage by default
(global setting `secondary.storage.capacity.threshold`, default `0.90`); free space or raise the threshold.

## Hypervisor CPU model (KVM)

SAP HANA requires CPU features such as AVX/AVX2 that the default QEMU CPU model hides. On every KVM host set
`guest.cpu.mode=host-model` (or `host-passthrough`) in `/etc/cloudstack/agent/agent.properties` and restart
`cloudstack-agent`. Instances started before the change need a stop/start.

## Offerings and network

- Compute offering with enough memory for the HANA host plan (e.g. 4 vCPU / 32 GB for small test systems).
- Disk offering with custom size (default name `Custom`); its storage tags select the primary storage of the data
  volumes. Any primary storage type works (LINSTOR, Ceph, NFS, CLVM, ...).
- A guest network whose VMs reach the execution node of the console via SSH (directly or through a bastion host) and,
  for registration and package installation, the update source (SUSE Customer Center, RMT/SMT, or the RHEL CDN/Satellite).

## High Availability fencing

`sap_vm_provision_cloudstack_ha_fencing_method`:

- `sbd_devices` (default): shared block device(s) already visible in both HANA VMs (e.g. iSCSI/multipath LUN), works
  with any hypervisor and storage. Enter the device paths as seen in the VMs.
- `sbd_linstor_kvm`: the provisioning creates a DRBD dual-primary LINSTOR resource and attaches it to both KVM domains
  with `virsh`. Requires KVM, LINSTOR primary storage, a root admin API user and SSH (root or passwordless sudo) from
  the console's execution node to the KVM hosts.
- `none`: no fencing by the provisioning (define e.g. `sap_ha_pacemaker_cluster_stonith_custom` yourself).

## Credentials

Enter the API URL, API key and secret key on the *Apache CloudStack* tab of the provider connection settings, together
with your Owner tag. The VMs and their volumes are tagged `Owner=<value>`; the inventory and *Terminate* only act on
resources with your Owner tag.
