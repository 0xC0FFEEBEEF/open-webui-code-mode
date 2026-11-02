#!/usr/bin/env python3
"""slerm-job runner: runs INSIDE the codebuild container on a compute node (Python 3.12, Ubuntu 24.04).

Mounts (made by job.sh):  /job/spec.json, /job/input.tar.gz  read-only  - what the VM sent
                          /work   this chat's project copy on the node (kept between jobs, so target/, node_modules/
                                  and other build output survive and builds are incremental)
                          /cache  package caches shared by jobs on this node (cargo, npm, pip)
                          /out    result.json + out.tar.gz, read back by the VM through slerm-job-server
1. sync /work to the project the VM sent: files the previous job's upload had but this one lacks are deleted; files
   the build made (never uploaded) stay. Extraction uses tarfile's "data" filter (no absolute paths, no "..", no links
   leaving /work, no device files).
2. run the command with bash in /work/<cwd>, output straight to the Slurm log.
3. pack the --get paths (relative to the command's folder, must stay inside /work) into /out/out.tar.gz.
"""
import json, os, shutil, signal, subprocess, sys, tarfile, time

WORK, SPEC, INPUT, OUT = "/work", "/job/spec.json", "/job/input.tar.gz", "/out"
MANIFEST = os.path.join(WORK, ".slerm-job", "manifest.json")
MAX_OUT = 2 * 1024 ** 3


def say(msg):
    print("[slerm-job] " + msg, flush=True)


def inside(path, root=WORK):
    real = os.path.realpath(path)
    return real == root or real.startswith(root + "/")


def result(**kw):
    with open(os.path.join(OUT, "result.json.tmp"), "w") as f:
        json.dump(kw, f)
    os.replace(os.path.join(OUT, "result.json.tmp"), os.path.join(OUT, "result.json"))


def sync():
    with tarfile.open(INPUT) as tf:
        members = tf.getmembers()
        names = sorted({m.name.lstrip("./") for m in members if not m.isdir()} - {""})
        try:
            old = set(json.load(open(MANIFEST)))
        except (OSError, ValueError):
            old = set()
        gone = 0
        for name in old - set(names):
            p = os.path.join(WORK, name)
            if os.path.lexists(p) and inside(os.path.dirname(p)) and not os.path.isdir(p):
                os.unlink(p)
                gone += 1
        tf.extractall(WORK, filter="data")
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(names, f)
    return len(names), gone


def pack(cwd, paths):
    missing, refused, files, size = [], [], 0, 0
    picked = []
    for g in paths:
        p = os.path.normpath(os.path.join(cwd, g))
        if not os.path.lexists(p):
            missing.append(g)
            continue
        if not inside(p):           # a symlink out of the project; the server already refused ".." and "/"
            refused.append(g)
            continue
        picked.append((p, g))
        for root, dirs, fs in os.walk(p) if os.path.isdir(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]:
            for n in fs:
                fp = os.path.join(root, n)
                if os.path.isfile(fp) and not os.path.islink(fp):
                    files += 1
                    size += os.path.getsize(fp)
    if size > MAX_OUT:
        say("outputs are %.1f GB, over the 2 GB limit; nothing copied back" % (size / 1024 ** 3))
        return {"missing": missing, "refused": refused, "files": 0, "bytes": 0, "too_big": size}
    if picked:
        with tarfile.open(os.path.join(OUT, "out.tar.gz"), "w:gz", compresslevel=3) as tf:
            for p, g in picked:
                tf.add(p, arcname=os.path.normpath(g))
    return {"missing": missing, "refused": refused, "files": files, "bytes": size}


def rust_home():
    """A writable rustup home in this node's cache. A project's rust-toolchain.toml can ask for a version or
    component the image lacks, and the image's /opt/rust/rustup is read-only. The image's toolchains are copied
    in first (no download for those); anything else rustup fetches once per node and keeps."""
    src, dst = "/opt/rust/rustup", "/cache/rustup"
    if not os.path.isdir(src):
        return None
    try:
        for sub in ("toolchains", "update-hashes"):
            os.makedirs(os.path.join(dst, sub), exist_ok=True)
            s = os.path.join(src, sub)
            for n in sorted(os.listdir(s)) if os.path.isdir(s) else []:
                d = os.path.join(dst, sub, n)
                if os.path.lexists(d):
                    continue
                tmp = "%s.seed-%d" % (d, os.getpid())
                subprocess.run(["cp", "-a", os.path.join(s, n), tmp], check=True)
                try:
                    os.rename(tmp, d)
                except OSError:             # another job on this node seeded it first
                    shutil.rmtree(tmp, ignore_errors=True) if os.path.isdir(tmp) else os.unlink(tmp)
        mark = os.path.join(dst, ".seeded-from-image")
        if not os.path.exists(mark):    # same default toolchain as the image
            shutil.copy(os.path.join(src, "settings.toml"), os.path.join(dst, "settings.toml"))
            open(mark, "w").close()
        return dst
    except Exception as e:
        say("rust: could not set up %s (%s); using the image's read-only toolchains" % (dst, e))
        return src


def main():
    spec = json.load(open(SPEC))
    t0 = time.time()
    node, cpus, mem = os.environ.get("SJ_NODE", "?"), os.environ.get("SJ_CPUS", "1"), os.environ.get("SJ_MEM", "?")
    n, gone = sync()
    cwd = os.path.normpath(os.path.join(WORK, spec.get("cwd") or "."))
    if not inside(cwd):
        say("refusing: the command folder is outside the project")
        result(exit=2, error="cwd outside project", node=node)
        return 2
    os.makedirs(cwd, exist_ok=True)
    say("on %s with %s CPUs and %s MB: %d files synced%s (%.1f s)" % (
        node, cpus, mem, n, ", %d removed" % gone if gone else "", time.time() - t0))
    say("$ " + spec["cmd"])
    env = dict(os.environ)
    env.update({
        "HOME": "/tmp/home", "CARGO_HOME": "/cache/cargo", "npm_config_cache": "/cache/npm",
        "PIP_CACHE_DIR": "/cache/pip", "MAKEFLAGS": "-j" + cpus, "CARGO_BUILD_JOBS": cpus,
        "CMAKE_BUILD_PARALLEL_LEVEL": cpus, "CI": "1", "TERM": "dumb", "NO_COLOR": "1",
        "PATH": "/opt/rust/cargo/bin:/cache/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    rh = rust_home()
    if rh:
        env["RUSTUP_HOME"] = rh
    os.makedirs("/tmp/home", exist_ok=True)
    proc = subprocess.Popen(["bash", "-c", spec["cmd"]], cwd=cwd, env=env, start_new_session=True)

    def stop(sig, _frame):          # Slurm time limit or scancel: stop the build, still say what happened
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        say("stopped (%s) after %.0f s" % ("time limit or cancel", time.time() - t0))
        result(exit=None, stopped=True, seconds=round(time.time() - t0, 1), node=node)
        sys.exit(143)
    signal.signal(signal.SIGTERM, stop)
    rc = proc.wait()
    took = time.time() - t0
    outs = pack(cwd, spec.get("get") or []) if spec.get("get") else {}
    say("exit %d after %.1f s on %s" % (rc, took, node))
    for g in outs.get("missing", []):
        say("--get %s: not found after the build" % g)
    for g in outs.get("refused", []):
        say("--get %s: refused (outside the project)" % g)
    result(exit=rc, seconds=round(took, 1), node=node, outputs=outs)
    return rc


if __name__ == "__main__":
    sys.exit(main())
