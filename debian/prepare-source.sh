#!/bin/sh
# Fetch what the offline build needs and git doesn't track: Go toolchain
# tarballs, restic binaries, vendor/ and the prebuilt webui/dist.
# Then: dpkg-buildpackage -us -uc
set -eu
cd "$(dirname "$0")/.."

mkdir -p go restic .gotoolchain .nodetoolchain
for arch in amd64 arm64; do
	[ -f go/go1.26.11.linux-$arch.tar.gz ] || curl -fL -o go/go1.26.11.linux-$arch.tar.gz \
		https://go.dev/dl/go1.26.11.linux-$arch.tar.gz
	[ -f restic/restic_0.19.1_linux_$arch.bz2 ] || curl -fL -o restic/restic_0.19.1_linux_$arch.bz2 \
		https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_linux_$arch.bz2
done

[ -x .gotoolchain/bin/go ] || tar -C .gotoolchain --strip-components=1 -xf go/go1.26.11.linux-amd64.tar.gz
GOTOOLCHAIN=local .gotoolchain/bin/go mod vendor

[ -x .nodetoolchain/bin/node ] || curl -fL https://nodejs.org/dist/v22.23.5/node-v22.23.5-linux-x64.tar.xz \
	| tar -C .nodetoolchain --strip-components=1 -xJ
PATH="$PWD/.nodetoolchain/bin:$PATH"
cd webui && npx --yes pnpm@9 install --frozen-lockfile && npx --yes pnpm@9 run build
