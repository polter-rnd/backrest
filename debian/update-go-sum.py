#!/usr/bin/python3 -I
"""Regenerate go.sum for the OBS scm-sync build.

OBS (obs-build PBuild/RemoteAssets.pm, golang_parse/golang_fetch) turns every
go.sum entry into a build-gomodcache/ GOPROXY asset, fetched server-side and
checked against its h1: hash before the offline build. To get the Go toolchain
and restic the same way, this script appends their module hashes to upstream's
go.sum, after tying each module to an upstream-verified artifact:

  Go toolchain  go1.X.linux-ARCH.tar.gz checked against the sha256 pinned below
                and the one go.dev publishes; the golang.org/toolchain module
                zip (hash from sum.golang.org, verified by `go mod download`)
                must contain exactly the tarball's files.
  restic        SHA256SUMS checked with gpgv against restic's release key (the
                one Backrest itself embeds in internal/resticinstaller/verify.go),
                source tarball checked against SHA256SUMS and the pin below; the
                github.com/restic/restic module zip must contain exactly the
                tarball's files. restic's go.sum is taken from that tarball.

Usage: debian/update-go-sum.py <upstream ref, e.g. upstream/main or v1.14.1>
"""
import hashlib, io, json, os, re, subprocess, sys, tarfile, tempfile, urllib.request, zipfile

# Recorded upstream checksums. Update together with GO_VERSION/RESTIC_VERSION in debian/rules.
GO_SHA256 = {
    "go1.26.11.linux-amd64.tar.gz": "9849f4e04ee35303714e34fbc3ac8ab44ea3e448a95aa182f91e3b9b79c710ec",
    "go1.26.11.linux-arm64.tar.gz": "6c2f29b458b5add0835f18b776f69c8881a6d88428f1484a6da55755e620757e",
}
RESTIC_SHA256 = {
    "restic-0.19.1.tar.gz": "bb9b1a19040744d26d8a79be029d4e6b189c45ccc9d8831d7fe367d3c33df725",
}
ARCHES = ("amd64", "arm64")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def die(msg):
    sys.exit(f"error: {msg}")


def rules_var(name):
    m = re.search(rf"^{name} := (\S+)$", open(os.path.join(ROOT, "debian/rules")).read(), re.M)
    return m.group(1) if m else die(f"{name} not found in debian/rules")


def fetch(url, dest):
    print(f"fetching {url}")
    urllib.request.urlretrieve(url, dest)
    return dest


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pin(path, pins):
    name = os.path.basename(path)
    if name not in pins:
        die(f"no recorded sha256 for {name}; add it to {__file__}")
    if sha256(path) != pins[name]:
        die(f"{name}: sha256 mismatch")
    print(f"ok: {name} sha256 matches recorded value")


def same_files(zip_path, zip_prefix, tar_path, tar_prefix):
    """Every file in the module zip must exist in the tarball with identical bytes."""
    with tarfile.open(tar_path) as t, zipfile.ZipFile(zip_path) as z:
        tm = {m.name[len(tar_prefix):]: m for m in t.getmembers()
              if m.isfile() and m.name.startswith(tar_prefix)}
        n = 0
        for i in z.infolist():
            if i.is_dir():
                continue
            p = i.filename[len(zip_prefix):]
            # golang.org/toolchain renames nested go.mod/go.sum to _go.mod/_go.sum
            if p not in tm and p.rsplit("/", 1)[-1] in ("_go.mod", "_go.sum"):
                p = p[:-7] + p[-6:]
            if p not in tm or z.read(i) != t.extractfile(tm[p]).read():
                die(f"{os.path.basename(zip_path)}: {p} differs from {os.path.basename(tar_path)}")
            n += 1
    print(f"ok: {os.path.basename(zip_path)} matches {os.path.basename(tar_path)} ({n} files)")


def main():
    if len(sys.argv) != 2:
        die(__doc__.strip().splitlines()[-1])
    upstream_gosum = subprocess.run(["git", "-C", ROOT, "show", f"{sys.argv[1]}:go.sum"],
                                    check=True, capture_output=True, text=True).stdout
    go_version, restic_version = rules_var("GO_VERSION"), rules_var("RESTIC_VERSION")
    host = {"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine]

    with tempfile.TemporaryDirectory() as tmp:
        # --- Go tarballs, checked against the recorded and the go.dev-published sha256 ---
        published = {f["filename"]: f["sha256"]
                     for r in json.load(urllib.request.urlopen("https://go.dev/dl/?mode=json&include=all"))
                     for f in r["files"]}
        tarballs = {}
        for arch in ARCHES:
            name = f"go{go_version}.linux-{arch}.tar.gz"
            tarballs[arch] = fetch(f"https://go.dev/dl/{name}", os.path.join(tmp, name))
            check_pin(tarballs[arch], GO_SHA256)
            if published.get(name) != GO_SHA256[name]:
                die(f"{name}: recorded sha256 differs from go.dev")
        with tarfile.open(tarballs[host]) as t:
            t.extractall(tmp, filter="tar")
        go = os.path.join(tmp, "go/bin/go")

        # --- Module hashes via `go mod download`, which verifies them against sum.golang.org ---
        mods = [f"golang.org/toolchain@v0.0.1-go{go_version}.linux-{a}" for a in ARCHES]
        mods.append(f"github.com/restic/restic@v{restic_version}")
        moddir = os.path.join(tmp, "mod")
        os.mkdir(moddir)
        env = dict(os.environ, GOMODCACHE=os.path.join(tmp, "gomodcache"), GOTOOLCHAIN="local",
                   GOFLAGS="-mod=mod", GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org")
        out = subprocess.run([go, "mod", "download", "-json", *mods], cwd=moddir, env=env,
                             check=True, capture_output=True, text=True).stdout
        info = {}
        for doc in json.load(io.StringIO("[" + re.sub(r"}\s*{", "},{", out) + "]")):
            if doc.get("Error"):
                die(doc["Error"])
            info[doc["Path"] + "@" + doc["Version"]] = doc
        lines = []
        for d in info.values():
            lines.append(f'{d["Path"]} {d["Version"]} {d["Sum"]}')
            lines.append(f'{d["Path"]} {d["Version"]}/go.mod {d["GoModSum"]}')

        for arch in ARCHES:
            m = info[f"golang.org/toolchain@v0.0.1-go{go_version}.linux-{arch}"]
            same_files(m["Zip"], f'{m["Path"]}@{m["Version"]}/', tarballs[arch], "go/")

        # --- restic: signed SHA256SUMS -> source tarball -> module zip ---
        base = f"https://github.com/restic/restic/releases/download/v{restic_version}"
        sums = fetch(f"{base}/SHA256SUMS", os.path.join(tmp, "SHA256SUMS"))
        sig = fetch(f"{base}/SHA256SUMS.asc", os.path.join(tmp, "SHA256SUMS.asc"))
        verify_go = open(os.path.join(ROOT, "internal/resticinstaller/verify.go")).read()
        key = re.search(r"-----BEGIN PGP PUBLIC KEY BLOCK-----.*?-----END PGP PUBLIC KEY BLOCK-----",
                        verify_go, re.S).group(0)
        keyring = os.path.join(tmp, "restic.gpg")
        subprocess.run(["gpg", "--batch", "--dearmor", "-o", keyring], input=key.encode(), check=True)
        subprocess.run(["gpgv", "--keyring", keyring, sig, sums], check=True)
        print("ok: SHA256SUMS signature verified with restic's release key")
        name = f"restic-{restic_version}.tar.gz"
        src = fetch(f"{base}/{name}", os.path.join(tmp, name))
        listed = dict(reversed(l.split()) for l in open(sums) if l.strip())
        if listed.get(name) != sha256(src):
            die(f"{name}: sha256 not in signed SHA256SUMS")
        check_pin(src, RESTIC_SHA256)
        m = info[f"github.com/restic/restic@v{restic_version}"]
        same_files(m["Zip"], f'{m["Path"]}@{m["Version"]}/', src, f"restic-{restic_version}/")
        with tarfile.open(src) as t:
            restic_gosum = t.extractfile(f"restic-{restic_version}/go.sum").read().decode()

    merged = sorted(set((upstream_gosum + "\n".join(lines) + "\n" + restic_gosum).splitlines()) - {""})
    with open(os.path.join(ROOT, "go.sum"), "w") as f:
        f.write("\n".join(merged) + "\n")
    print(f"go.sum: {len(merged)} lines")


if __name__ == "__main__":
    main()
