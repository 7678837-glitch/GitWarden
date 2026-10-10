#!/bin/sh
# git 通过 GIT_ASKPASS 调用：提示以 Username 开头时输出用户名，否则输出 secret。
case "$1" in
  Username*) printf '%s\n' "$GW_USERNAME" ;;
  *) printf '%s\n' "$GW_SECRET" ;;
esac
