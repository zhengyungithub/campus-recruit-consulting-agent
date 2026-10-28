#!/usr/bin/env bash
# 校招咨询 Agent —— 生产环境一键更新脚本
#
# 位置：服务器 /opt/campus-agent/deploy.sh
# 用法：sudo /opt/campus-agent/deploy.sh
#
# 流程：记录当前版本 -> 拉取最新代码 -> 依赖变化时才重装 -> 平滑重启 -> 健康检查
# 健康检查失败会自动回滚到上一个提交并打印服务日志。
set -euo pipefail

APP_ROOT=/opt/campus-agent
SRC="$APP_ROOT/src"
SERVICE=campus-agent
HEALTH_URL=http://127.0.0.1:8010/api/health

echo "[1/5] 记录当前版本"
BEFORE=$(git -C "$SRC" rev-parse --short HEAD)
echo "      当前: $BEFORE"

echo "[2/5] 拉取最新代码"
git -C "$SRC" pull --ff-only
AFTER=$(git -C "$SRC" rev-parse --short HEAD)
echo "      最新: $AFTER"

# 依赖只在 requirements.txt 变化时重装，避免每次全量下载
if [ "$BEFORE" != "$AFTER" ] && git -C "$SRC" diff --name-only "$BEFORE" "$AFTER" | grep -qx "requirements.txt"; then
  echo "[3/5] requirements.txt 有变化，安装依赖"
  "$SRC/venv/bin/pip" install -q -r "$SRC/requirements.txt" -i https://mirrors.cloud.tencent.com/pypi/simple
else
  echo "[3/5] 依赖无变化，跳过安装"
fi

echo "[4/5] 重启服务"
systemctl restart "$SERVICE"

echo "[5/5] 健康检查"
for i in $(seq 1 15); do
  sleep 2
  if body=$(curl -sf -m 3 "$HEALTH_URL"); then
    echo "      服务已就绪: $body"
    echo "完成：$BEFORE -> $AFTER"
    exit 0
  fi
  echo "      等待服务就绪... ($i)"
done

echo "健康检查失败，回滚到上一个版本"
git -C "$SRC" checkout "$BEFORE"
systemctl restart "$SERVICE"
journalctl -u "$SERVICE" -n 30 --no-pager
exit 1
