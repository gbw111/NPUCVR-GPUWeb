#!/bin/bash

# 检查参数
if [ "$#" -ne 4 ]; then
    echo "Usage: $0 <NodeName> <RemoteUser> <RemoteServer> <RemotePort>"
    exit 1
fi

NODE_NAME=$1
REMOTE_USER=$2
REMOTE_SERVER=$3
REMOTE_PORT=$4

# 获取脚本所在目录
SCRIPT_DIR=$(cd $(dirname $0); pwd)
cd $SCRIPT_DIR

WEB_ROOT="${GPU_MONITOR_REMOTE_ROOT:-/var/www/html/gpu}"
DATA_DIR="${GPU_MONITOR_REMOTE_DATA_DIR:-${WEB_ROOT}/data}"
DAILY_DIR="${GPU_MONITOR_REMOTE_DAILY_DIR:-${WEB_ROOT}/history/daily}"
LOCAL_DAILY_DIR="${SCRIPT_DIR}/history/daily"

SCP_OPTS=(-P "$REMOTE_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=5)
SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=5)

# 1. 执行 Python 脚本生成 JSON
# 确保 python 环境中有 psutil，如果没有可以用 venv 路径代替 python 命令
/usr/bin/python3 agent.py "$NODE_NAME"

if [ $? -ne 0 ]; then
    echo "Error: JSON generation failed."
    exit 1
fi

JSON_FILE="${NODE_NAME}.json"

# 2. 上传至汇总服务器
# 当前状态: /var/www/html/gpu/data/
# 每日归档: /var/www/html/gpu/history/daily/
mkdir_cmd=$(printf "mkdir -p %q %q" "$DATA_DIR" "$DAILY_DIR")
ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_SERVER}" "$mkdir_cmd"

if [ $? -ne 0 ]; then
    echo "Error: Failed to create remote directories."
    exit 1
fi

echo "Uploading $JSON_FILE to $REMOTE_SERVER..."

scp "${SCP_OPTS[@]}" "$JSON_FILE" "${REMOTE_USER}@${REMOTE_SERVER}:${DATA_DIR}/"

if [ $? -eq 0 ]; then
    echo "Success: Data uploaded."
else
    echo "Error: Upload failed."
    exit 1
fi

# 3. 补充上传 daily JSON。每次同步今天和昨天，避免跨天或单次失败造成归档缺口。
TODAY=$(date +%F)
YESTERDAY=$(date -d "yesterday" +%F)
DAILY_UPLOAD_FAILED=0

for DAY in "$YESTERDAY" "$TODAY"; do
    DAILY_FILE="${LOCAL_DAILY_DIR}/${NODE_NAME}_${DAY}.json"
    if [ ! -f "$DAILY_FILE" ]; then
        continue
    fi

    echo "Uploading daily archive $(basename "$DAILY_FILE") to $REMOTE_SERVER..."
    scp "${SCP_OPTS[@]}" "$DAILY_FILE" "${REMOTE_USER}@${REMOTE_SERVER}:${DAILY_DIR}/"
    if [ $? -ne 0 ]; then
        echo "Error: Daily archive upload failed: $DAILY_FILE"
        DAILY_UPLOAD_FAILED=1
    fi
done

if [ "$DAILY_UPLOAD_FAILED" -eq 0 ]; then
    echo "Success: Daily archives uploaded."
else
    exit 1
fi
