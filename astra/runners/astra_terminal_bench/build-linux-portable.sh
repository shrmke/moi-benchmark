#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: build-linux-portable.sh --arch amd64|arm64" >&2
}

architecture=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch)
      [[ $# -ge 2 ]] || {
        usage
        exit 2
      }
      architecture="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/../../.." && pwd)"
source_root="${ASTRA_SOURCE_ROOT:-$workspace_root/external/astra}"

case "$architecture" in
  amd64)
    docker_platform="linux/amd64"
    rust_target="x86_64-unknown-linux-musl"
    build_root="${ASTRA_LINUX_BUILD_ROOT:-$workspace_root/work/astra-linux-build-amd64}"
    ;;
  arm64)
    docker_platform="linux/arm64"
    rust_target="aarch64-unknown-linux-musl"
    build_root="${ASTRA_LINUX_BUILD_ROOT:-$workspace_root/work/astra-linux-build}"
    ;;
  *)
    usage
    exit 2
    ;;
esac

[[ -f "$source_root/Cargo.toml" ]] || {
  echo "Astra source checkout not found at $source_root" >&2
  exit 1
}

mkdir -p "$build_root/cargo-home" "$build_root/home" "$build_root/target/release"

docker run --rm \
  --platform "$docker_platform" \
  -e HOME=/out/home \
  -e CARGO_HOME=/out/cargo-home \
  -e CARGO_TARGET_DIR=/out/target \
  -e RUST_TARGET="$rust_target" \
  -e OUTPUT_UID="$(id -u)" \
  -e OUTPUT_GID="$(id -g)" \
  -v "$source_root:/src:ro" \
  -v "$build_root:/out" \
  -w /src \
  rust:1.97-bookworm \
  bash -euo pipefail -c '
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends musl-tools
    rustup target add "$RUST_TARGET"
    cargo build \
      --locked \
      --release \
      --no-default-features \
      --features release-vendored-openssl \
      --manifest-path /src/Cargo.toml \
      --target "$RUST_TARGET" \
      -p astra-cli \
      --bin astra
    install -m 0555 "/out/target/$RUST_TARGET/release/astra" /out/target/release/astra
    chown "$OUTPUT_UID:$OUTPUT_GID" /out/target/release/astra
  '

artifact="$build_root/target/release/astra"
[[ -x "$artifact" ]] || {
  echo "portable Linux $architecture Astra artifact was not created: $artifact" >&2
  exit 1
}

artifact_description="$(file "$artifact")"
if [[ "$artifact_description" != *"ELF 64-bit"* || "$artifact_description" == *"dynamically linked"* ]]; then
  echo "portable Astra artifact is not statically linked" >&2
  echo "$artifact_description" >&2
  exit 1
fi

file "$artifact"
shasum -a 256 "$artifact"
docker run --rm \
  --platform "$docker_platform" \
  --network none \
  --entrypoint /usr/local/bin/astra \
  -v "$artifact:/usr/local/bin/astra:ro" \
  alpine:3.22 \
  --version

printf 'ASTRA_TBENCH_LINUX_BINARY=%s\n' "$artifact"
