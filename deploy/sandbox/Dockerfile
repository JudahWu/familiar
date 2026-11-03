# Familiar 沙箱镜像
#
# 为什么需要它：OpenSandbox 默认用的 `python:*-slim` 是官方最小镜像，实测里面
# 只有 Python + bash + coreutils + apt-get —— **没有 git、curl，也没有 ssh 客户端**。
# 而 Agent 的 coding 流程（clone → 改 → commit → push → 开 PR）全依赖 git。
#
# 这个镜像只做两件事：
#   1. 补齐 git / curl / CA 证书这类基础工具；
#   2. 预置 Git 身份与 safe.directory，并把不含令牌的 askpass 脚本放到 PATH 上。
#
# **镜像里不含任何令牌**。令牌由后端在每次执行 git 命令时，通过
# RunCommandOpts.envs 注入到那条命令的进程环境里（见
# app/backends/opensandbox_backend.py 的 _git_envs_for）。
#
# 构建：scripts/build_sandbox_image.sh
#   或 docker build -f ops/sandbox/Dockerfile -t familiar/sandbox:latest ops/sandbox

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

# Debian 软件源镜像。默认走阿里云的 HTTPS 源，原因有两个：
#   1. 官方 deb.debian.org 走明文 HTTP，很多公司网络/代理会直接打断（返回 502）；
#   2. 国内拉取快得多，构建从几分钟降到十几秒。
# 想换回官方源：--build-arg APT_MIRROR=deb.debian.org
# 想用清华源：  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG APT_MIRROR=mirrors.aliyun.com

# git：Agent 的核心工具；curl：下载依赖/健康检查用；ca-certificates：HTTPS 根证书；
# openssh-client：ssh-keygen / scp 之类的轻量场景（不用于 Git 认证）。
RUN set -eux; \
    # 先换 security 再换主源，避免 debian-security 被前一条规则提前改掉
    sed -i \
        -e "s|http://deb.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" \
        -e "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g" \
        -e "s|http://security.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        openssh-client \
        less \
        tar \
        unzip; \
    rm -rf /var/lib/apt/lists/*

# Git 身份：沙箱里 commit 必须能拿到 author，否则报 “Author identity unknown”。
# safe.directory：/projects 是宿主目录挂进来的，属主一般是宿主 uid（501），
# 容器里以 root 跑时 git 会判定 “dubious ownership” 并拒绝操作。
RUN git config --system user.name "Familiar Agent" \
    && git config --system user.email "agent@familiar.local" \
    && git config --system safe.directory "*" \
    && git config --system init.defaultBranch main \
    && git config --system pull.rebase false

# askpass：只从环境变量读令牌，脚本本身不含任何凭据。
COPY git-askpass.sh /usr/local/bin/git-askpass.sh
RUN chmod 0700 /usr/local/bin/git-askpass.sh

# 版本标记，方便 health() 判断当前沙箱是不是这个镜像
RUN printf 'familiar-sandbox 1\n' > /etc/familiar-sandbox

WORKDIR /projects
CMD ["tail", "-f", "/dev/null"]
