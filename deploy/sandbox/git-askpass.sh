#!/bin/sh
# Git 非交互认证（askpass）脚本 —— 沙箱版。
#
# Git 在需要 HTTPS 凭据时会调用它，并把提示语作为 $1 传进来，例如：
#   "Username for 'https://github.com': "
#   "Password for 'https://oauth2@github.com': "
#
# 我们的做法是按提示语里的域名选择对应平台的令牌。令牌只从**环境变量**读，
# 由后端在每次执行 git 命令时通过 RunCommandOpts.envs 注入，退出即消失：
# 脚本本身、镜像层、容器文件系统里都不会留下凭据。
#
# 宿主的 LocalShellBackend 生成的是同一套逻辑（见 local_shell.py），
# 两处保持一致，避免换个运行模式就认证行为不一样。

case "$1" in
  *github.com*) platform=github ;;
  *gitlab*) platform=gitlab ;;
  *) platform=gitee ;;
esac

# 自建 GitLab 的域名不一定带 "gitlab" 字样，用 GITLAB_ASKPASS_HOST 兜底。
# 注意必须判空：空的匹配模式会匹配一切，把 Gitee 的请求也吞掉。
if [ "$platform" = gitee ] && [ -n "$GITLAB_ASKPASS_HOST" ]; then
  case "$1" in
    *"$GITLAB_ASKPASS_HOST"*) platform=gitlab ;;
  esac
fi

case "$platform" in
  github)
    askpass_user="${GITHUB_ASKPASS_USERNAME:-x-access-token}"
    askpass_token="$GITHUB_ASKPASS_TOKEN"
    ;;
  gitlab)
    askpass_user="${GITLAB_ASKPASS_USERNAME:-oauth2}"
    askpass_token="$GITLAB_ASKPASS_TOKEN"
    ;;
  *)
    askpass_user="${GITEE_ASKPASS_USERNAME:-oauth2}"
    askpass_token="$GITEE_ASKPASS_TOKEN"
    ;;
esac

case "$1" in
  *sername*|*Username*) printf '%s' "$askpass_user" ;;
  *) printf '%s' "$askpass_token" ;;
esac
