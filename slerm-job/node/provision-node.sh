#!/bin/bash
# provision-node.sh - make a slerm compute node ready for Code mode build jobs (slerm-job). Idempotent. Run as root.
# Copy of ~/slerm-codebuild/node/ (Ghost) also at /apps/codebuild/node/ so any node can run it:
#   sudo bash /apps/codebuild/node/provision-node.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# 1. user (uid/gid 1102 everywhere; home is the NFS /home/codebuild made on the head)
getent group codebuild >/dev/null || groupadd -g 1102 codebuild
id codebuild >/dev/null 2>&1 || useradd -u 1102 -g 1102 -M -d /home/codebuild -s /bin/bash -c "Code mode build jobs" codebuild
passwd -l codebuild >/dev/null
[ "$(id -u codebuild)" = 1102 ] || { echo "codebuild has the wrong uid" >&2; exit 1; }
# 2. apptainer (EPEL; slurm* stays excluded there)
rpm -q apptainer squashfuse fuse-overlayfs >/dev/null 2>&1 || dnf -y -q install apptainer squashfuse fuse-overlayfs
# 3. node-local work area (project copies, caches, local image copy)
install -d -o codebuild -g codebuild -m 700 /var/tmp/codebuild
# 4. fence (nft by uid), loaded before slurmd starts
install -d -m 755 /etc/codebuild
install -m 644 "$here/fence.nft" /etc/codebuild/fence.nft
install -m 644 "$here/codebuild-fence.service" /etc/systemd/system/codebuild-fence.service
install -d -m 755 /etc/systemd/system/slurmd.service.d
printf '[Unit]\nWants=codebuild-fence.service\nAfter=codebuild-fence.service\n' > /etc/systemd/system/slurmd.service.d/20-codebuild-fence.conf
systemctl daemon-reload
systemctl enable codebuild-fence.service >/dev/null 2>&1
systemctl restart codebuild-fence.service
nft list table inet codebuild_fence >/dev/null && echo "$(hostname -s): codebuild ready ($(rpm -q --qf '%{VERSION}' apptainer), fence loaded)"
