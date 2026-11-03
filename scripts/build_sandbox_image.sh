#!/usr/bin/env bash
# 构建 Familiar 沙箱镜像（OpenSandbox 用的那个）。
#
# 用法：
#   scripts/build_sandbox_image.sh            # 构建 familiar/sandbox:latest
#   scripts/build_sandbox_image.sh my/tag     # 自定义 tag
#
# 构建完成后记得让 .env 的 OPENSANDBOX_IMAGE 指向同一个 tag，
# 否则新建的沙箱还是旧镜像（python:*-slim，没有 git）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-familiar/sandbox:latest}"
# Debian 源镜像。官方源走明文 HTTP，很多网络会把它打断成 502，所以默认用阿里云。
APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"

echo "==> 构建沙箱镜像: ${TAG} (apt 源: ${APT_MIRROR})"
docker build \
  --build-arg "APT_MIRROR=${APT_MIRROR}" \
  -f "${ROOT}/deploy/sandbox/Dockerfile" \
  -t "${TAG}" \
  "${ROOT}/deploy/sandbox"

echo "==> 自检：镜像里必须有 git / curl"
docker run --rm --entrypoint sh "${TAG}" -c '
  set -e
  git --version
  curl --version | head -1
  test -x /usr/local/bin/git-askpass.sh && echo "askpass ok"
  git config --system --get user.email
  git config --system --get safe.directory
'

echo
echo "==> 完成。把下面这行写进 .env（或用默认值就不用改）："
echo "    OPENSANDBOX_IMAGE=${TAG}"
