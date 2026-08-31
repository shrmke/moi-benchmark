#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
workspace_root=$(cd "$script_dir/../../.." && pwd)
source_root="${ASTRA_SOURCE_ROOT:-$workspace_root/external/astra}"
build_root="${ASTRA_LINUX_BUILD_ROOT:-$workspace_root/work/astra-linux-build-amd64}"

if [[ ! -f "$source_root/Cargo.toml" ]]; then
    echo "Astra source checkout not found at $source_root" >&2
    exit 1
fi

mkdir -p "$build_root/cargo-home" "$build_root/home" "$build_root/target"

docker run --rm \
    --platform linux/amd64 \
    --user "$(id -u):$(id -g)" \
    -e HOME=/out/home \
    -e CARGO_HOME=/out/cargo-home \
    -e CARGO_TARGET_DIR=/out/target \
    -v "$source_root:/src:ro" \
    -v "$build_root:/out" \
    -w /src \
    rust:1.97-bookworm \
    cargo build \
        --locked \
        --release \
        --no-default-features \
        --features release-vendored-openssl \
        --manifest-path /src/Cargo.toml \
        -p astra-cli \
        --bin astra

artifact="$build_root/target/release/astra"
if [[ ! -x "$artifact" ]]; then
    echo "Linux amd64 Astra artifact was not created at $artifact" >&2
    exit 1
fi

file "$artifact"
shasum -a 256 "$artifact"
docker run --rm \
    --platform linux/amd64 \
    -v "$artifact:/usr/local/bin/astra:ro" \
    ubuntu:24.04 \
    /usr/local/bin/astra --version

printf 'ASTRA_TBENCH_LINUX_BINARY=%s\n' "$artifact"
