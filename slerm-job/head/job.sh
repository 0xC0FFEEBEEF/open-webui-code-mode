#!/bin/bash
# slerm-job batch script: runs on a compute node as codebuild, sent by slerm-job-server (sbatch copies it, so the
# installed /apps/codebuild/job.sh only matters at submit time).  Args: JOBDIR CHAT
# Everything the model wrote runs inside the codebuild image with only this chat's folder, the package caches and
# the job's out/ mounted. Source: ~/slerm-codebuild/head/job.sh on Ghost.
set -u
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin HOME=/home/codebuild
JOBDIR=${1:?jobdir}
CHAT=${2:?chat}
BASE=/var/tmp/codebuild
SIF_SRC=/apps/codebuild/codebuild.sif
RUNNER=/apps/codebuild/runner.py
NODE=${SLURMD_NODENAME:-$(hostname -s)}
say() { printf '[slerm-job] %s\n' "$*"; }
fail() {
    say "$1"
    printf '{"exit": %s, "error": "%s", "node": "%s"}\n' "${2:-125}" "$1" "$NODE" > "$JOBDIR/out/result.json"
    exit "${2:-125}"
}

# 1. The fence must be up: for this uid the head's ssh port has to be unreachable.
if timeout 3 bash -c 'exec 3<>/dev/tcp/slerm-head/22' 2>/dev/null; then
    fail "refusing to run: the LAN fence (codebuild-fence.service) is not loaded on $NODE"
fi
mkdir -p "$BASE"/img "$BASE"/chats "$BASE"/cache "$BASE"/tmp
chmod 700 "$BASE"

# 2. A node-local copy of the image (NFS reads of a 2 GB squashfs on every compile would be slow).
src=$(readlink -f "$SIF_SRC") && [ -s "$src" ] || fail "no build image at $SIF_SRC"
img="$BASE/img/$(basename "$src")"
if [ ! -s "$img" ]; then
    say "first job on $NODE with this image: copying $(basename "$src") ($(du -h "$src" | cut -f1))"
    cp "$src" "$img.$$" && mv -f "$img.$$" "$img" || fail "could not copy the build image"
    ls -t "$BASE"/img/*.sif | tail -n +3 | xargs -r rm -f
fi

# 3. This chat's folder on this node: kept between jobs (incremental builds), one job at a time per chat.
W="$BASE/chats/$CHAT"
mkdir -p "$W" && touch "$W"
exec 9>"$BASE/chats/.$CHAT.lock"
flock -n 9 || { say "another job for this chat is running on $NODE; waiting for it"; flock 9; }
prune() {   # remove chat folders that no running job holds
    local d c
    for d in "$BASE"/chats/*/; do
        d=${d%/}; c=$(basename "$d")
        [ "$d" = "$W" ] && continue
        [ "$1" = all ] || [ -n "$(find "$d" -maxdepth 0 -mtime +3)" ] || continue
        ( flock -n 8 && rm -rf "$d" ) 8>"$BASE/chats/.$c.lock"
    done
}
prune old
if [ "$(df --output=avail -k "$BASE" | tail -1)" -lt 5242880 ]; then
    say "under 5 GB free on $NODE: clearing other chats' folders and the package caches"
    prune all
    rm -rf "$BASE"/cache/*
fi

# 4. Run.
T="$BASE/tmp/${SLURM_JOB_ID:-$$}"
mkdir -p "$T"
trap 'rm -rf "$T"' EXIT
apptainer -s exec --containall --no-mount home,cwd,hostfs,bind-paths --workdir "$T" \
    --bind "$W:/work" --bind "$BASE/cache:/cache" --bind "$JOBDIR/out:/out" \
    --bind "$JOBDIR/spec.json:/job/spec.json:ro" --bind "$JOBDIR/input.tar.gz:/job/input.tar.gz:ro" \
    --bind "$RUNNER:/opt/slerm-job/runner.py:ro" \
    --bind /etc/resolv.conf:/etc/resolv.conf:ro --bind /etc/hosts:/etc/hosts:ro \
    --env "SJ_NODE=$NODE,SJ_CPUS=${SLURM_CPUS_PER_TASK:-1},SJ_MEM=${SLURM_MEM_PER_NODE:-?}" \
    --pwd /work "$img" python3 /opt/slerm-job/runner.py
rc=$?
[ -s "$JOBDIR/out/result.json" ] || fail "the build container did not start or died (exit $rc)" "$([ "$rc" -eq 0 ] && echo 125 || echo "$rc")"
exit "$rc"
