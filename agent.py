import json
import time
import sys
import subprocess
import os
import psutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_AGENT_CONFIG = {
    "disk_mounts": ["/home"],
    "history_days": 7,
    "sample_interval_sec": 60,
    "min_user_gpus": 1,
    "exclude_users": ["root"],
    "daily_archive": True
}

def load_agent_config():
    cfg = DEFAULT_AGENT_CONFIG.copy()
    config_path = os.path.join(SCRIPT_DIR, "config", "agent.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                file_cfg = json.load(f)
            if isinstance(file_cfg, dict):
                for k, v in file_cfg.items():
                    if v is not None:
                        cfg[k] = v
        except Exception as e:
            print(f"Warning: failed to load config/agent.json: {e}")

    env_disks = os.getenv("GPU_MONITOR_DISKS")
    if env_disks:
        mounts = [m.strip() for m in env_disks.split(",") if m.strip()]
        if mounts:
            cfg["disk_mounts"] = mounts

    env_days = os.getenv("GPU_MONITOR_HISTORY_DAYS")
    if env_days and env_days.isdigit():
        cfg["history_days"] = int(env_days)

    env_interval = os.getenv("GPU_MONITOR_SAMPLE_INTERVAL_SEC")
    if env_interval and env_interval.isdigit():
        cfg["sample_interval_sec"] = int(env_interval)

    env_min_gpus = os.getenv("GPU_MONITOR_MIN_USER_GPUS")
    if env_min_gpus and env_min_gpus.isdigit():
        cfg["min_user_gpus"] = int(env_min_gpus)

    env_exclude = os.getenv("GPU_MONITOR_EXCLUDE_USERS")
    if env_exclude:
        users = [u.strip() for u in env_exclude.split(",") if u.strip()]
        if users:
            cfg["exclude_users"] = users

    env_daily_archive = os.getenv("GPU_MONITOR_DAILY_ARCHIVE")
    if env_daily_archive is not None:
        cfg["daily_archive"] = env_daily_archive.strip().lower() not in ("0", "false", "no", "off")

    return cfg

def get_gpu_info():
    """解析 nvidia-smi 输出获取 GPU 和进程信息 (修复版: 使用 UUID 匹配)"""
    gpus = []
    gpu_map = {} # 用于通过 UUID 快速找到 GPU 对象

    try:
        # 1. 获取 GPU 基础信息
        # 增加 'uuid' 字段，用于后续匹配进程
        # index, uuid, name, memory.total, memory.used, utilization.gpu
        cmd = "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits"
        output = subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
        
        lines = output.split('\n')
        for line in lines:
            if not line: continue
            # 注意：这里 split 必须与上面的 query-gpu 顺序一致
            parts = [x.strip() for x in line.split(',')]
            if len(parts) < 6: continue
            
            idx, uuid, name, total, used, util = parts
            
            # 计算显存百分比
            total_mb = int(total)
            used_mb = int(used)
            vram_percent = round((used_mb / total_mb) * 100) if total_mb > 0 else 0
            
            gpu_data = {
                "id": int(idx),
                "uuid": uuid,   # 新增 uuid 字段
                "name": name,
                "vram_total_mb": total_mb,
                "vram_used_mb": used_mb,
                "vram_percent": vram_percent,
                "util_percent": int(util),
                "processes": [] 
            }
            gpus.append(gpu_data)
            gpu_map[uuid] = gpu_data # 建立 UUID -> GPU 对象的映射
            
        # 2. 获取 GPU 进程信息
        # 修复: 将 'gpu_index' 改为 'gpu_uuid'
        # 修复: 确保使用标准字段 'used_gpu_memory' (部分驱动不支持 'used_memory')
        p_cmd = "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_gpu_memory --format=csv,noheader,nounits"
        try:
            p_output = subprocess.check_output(p_cmd, shell=True).decode('utf-8').strip()
        except subprocess.CalledProcessError:
            # 如果没有运行中的 GPU 进程，nvidia-smi 有时会返回非零状态或空，这里做容错
            p_output = ""
        
        if p_output:
            for p_line in p_output.split('\n'):
                if not p_line: continue
                parts = [x.strip() for x in p_line.split(',')]
                if len(parts) < 3: continue

                p_uuid, pid, used_mem = parts
                
                # 获取用户名
                try:
                    user = psutil.Process(int(pid)).username()
                except:
                    user = "unknown"
                
                # 使用 UUID 找到对应的 GPU 对象
                if p_uuid in gpu_map:
                    target_gpu = gpu_map[p_uuid]
                    
                    # 计算该进程显存占用百分比
                    p_ram_percent = 0
                    if target_gpu['vram_total_mb'] > 0:
                        try:
                            used_mem_val = int(used_mem) # 有时候可能是 N/A
                        except:
                            used_mem_val = 0
                        p_ram_percent = round((used_mem_val / target_gpu['vram_total_mb']) * 100)
                        
                    target_gpu['processes'].append({
                        "pid": int(pid),
                        "user": user,
                        "ram_percent": p_ram_percent
                    })

    except Exception as e:
        # 打印错误但不中断脚本，防止单次采集失败导致崩溃
        print(f"Error collecting GPU info: {e}")
        import traceback
        traceback.print_exc()
    
    return gpus

def _unique_mounts(mounts):
    seen = set()
    result = []
    for m in mounts:
        if m in seen:
            continue
        seen.add(m)
        result.append(m)
    return result

def _disk_usage_gb(mount):
    usage = psutil.disk_usage(mount)
    total_gb = round(usage.total / (1024 ** 3), 1)
    used_gb = round(usage.used / (1024 ** 3), 1)
    return {
        "mount": mount,
        "used_percent": round(usage.percent),
        "used_gb": used_gb,
        "total_gb": total_gb
    }

def get_system_info(disk_mounts):
    """获取 CPU, RAM, 磁盘信息"""
    try:
        disks = []
        for mount in _unique_mounts(disk_mounts or []):
            if not os.path.exists(mount):
                continue
            if not os.path.isdir(mount):
                continue
            try:
                disks.append(_disk_usage_gb(mount))
            except Exception:
                continue

        ssd_percent = 0
        try:
            if os.path.exists("/home"):
                ssd_percent = round(psutil.disk_usage('/home').percent)
            else:
                ssd_percent = round(psutil.disk_usage('/').percent)
        except Exception:
            ssd_percent = 0

        return {
            "cpu_percent": round(psutil.cpu_percent(interval=1)),
            "ram_percent": round(psutil.virtual_memory().percent),
            "ssd_percent": ssd_percent,
            "disks": disks
        }
    except Exception as e:
        print(f"Error collecting System info: {e}")
        return {"cpu_percent": 0, "ram_percent": 0, "ssd_percent": 0, "disks": []}

def _collect_user_snapshot(gpus, exclude_users):
    users = {}
    active_gpus = 0
    total_gpus = len(gpus)

    for gpu in gpus:
        gpu_users = set()
        for proc in gpu.get("processes", []):
            user = proc.get("user") or "unknown"
            if user in exclude_users:
                continue
            gpu_users.add(user)

        if gpu_users:
            active_gpus += 1
            for user in gpu_users:
                users[user] = users.get(user, 0) + 1

    return users, active_gpus, total_gpus

def _load_history_lines(path, since_ts, metric=None):
    records = []
    if not os.path.exists(path):
        return records
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if metric and rec.get("metric") != metric:
                    continue
                if rec.get("ts", 0) >= since_ts:
                    records.append(rec)
    except Exception as e:
        print(f"Warning: failed to read history {path}: {e}")
    return records

def _write_history_lines(path, records):
    try:
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Warning: failed to write history {path}: {e}")

def build_daily_usage_archive(node_name, records, cfg, total_gpus_latest, now_ts=None):
    """
    Generate per-day, per-user GPU usage archive for a single node.
    Output:
        history/daily/{node_name}_YYYY-MM-DD.json
    Metric:
        gpu_hours = sum(user_gpu_count_per_sample * sample_interval_sec) / 3600
    """
    if now_ts is None:
        now_ts = int(time.time())

    day_text = time.strftime("%Y-%m-%d", time.localtime(now_ts))
    day_start = int(time.mktime(time.strptime(day_text + " 00:00:00", "%Y-%m-%d %H:%M:%S")))
    day_end = day_start + 24 * 3600
    interval_sec = int(cfg.get("sample_interval_sec", 60))
    min_user_gpus = int(cfg.get("min_user_gpus", 1))

    stats = {}
    for rec in records:
        ts = int(rec.get("ts", 0))
        if ts < day_start or ts >= day_end:
            continue

        rec_interval = int(rec.get("interval_sec", interval_sec))
        for user, gpu_count in rec.get("users", {}).items():
            try:
                gpu_count = int(gpu_count)
            except Exception:
                continue
            if gpu_count < min_user_gpus:
                continue

            entry = stats.setdefault(user, {
                "samples": 0,
                "gpu_seconds": 0.0,
                "active_seconds": 0,
                "max_gpus": 0,
                "last_ts": 0
            })
            entry["samples"] += 1
            entry["gpu_seconds"] += gpu_count * rec_interval
            entry["active_seconds"] += rec_interval
            entry["max_gpus"] = max(entry["max_gpus"], gpu_count)
            entry["last_ts"] = max(entry["last_ts"], ts)

    users = []
    for user, entry in stats.items():
        users.append({
            "user": user,
            "active_hours": round(entry["active_seconds"] / 3600, 4),
            "gpu_hours": round(entry["gpu_seconds"] / 3600, 4),
            "max_gpus": int(entry["max_gpus"]),
            "samples": entry["samples"],
            "last_seen": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(entry["last_ts"])
            ) if entry["last_ts"] else None
        })

    users.sort(key=lambda x: (x["gpu_hours"], x["active_hours"]), reverse=True)
    daily_data = {
        "node": node_name,
        "date": day_text,
        "sample_interval_sec": interval_sec,
        "total_gpus": total_gpus_latest,
        "users": users
    }

    daily_dir = os.path.join(SCRIPT_DIR, "history", "daily")
    os.makedirs(daily_dir, exist_ok=True)
    daily_path = os.path.join(daily_dir, f"{node_name}_{day_text}.json")
    tmp_path = daily_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(daily_data, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, daily_path)
    return daily_path, daily_data

def build_usage_summary(node_name, gpus, cfg):
    history_days = int(cfg.get("history_days", 7))
    if history_days <= 0:
        return None

    history_dir = os.path.join(SCRIPT_DIR, "history")
    os.makedirs(history_dir, exist_ok=True)
    history_path = os.path.join(history_dir, f"{node_name}_usage.jsonl")

    now_ts = int(time.time())
    window_sec = history_days * 24 * 3600
    since_ts = now_ts - window_sec

    min_user_gpus = int(cfg.get("min_user_gpus", 1))
    exclude_users = set(cfg.get("exclude_users") or [])
    interval_sec = int(cfg.get("sample_interval_sec", 60))

    records = _load_history_lines(history_path, since_ts, metric="gpu_hours")

    snapshot_users, active_gpus, total_gpus = _collect_user_snapshot(gpus, exclude_users)
    record = {
        "ts": now_ts,
        "metric": "gpu_hours",
        "interval_sec": interval_sec,
        "users": snapshot_users,
        "active_gpus": active_gpus,
        "total_gpus": total_gpus
    }
    records.append(record)
    _write_history_lines(history_path, records)

    stats = {}
    total_samples = len(records)
    idle_samples = 0
    active_gpus_sum = 0
    active_samples = 0
    total_gpus_latest = total_gpus
    for rec in records:
        rec_users = rec.get("users", {})
        rec_interval = int(rec.get("interval_sec", interval_sec))
        ts = rec.get("ts", 0)
        rec_active_gpus = int(rec.get("active_gpus", 0))
        rec_total_gpus = int(rec.get("total_gpus", 0))
        if rec_total_gpus > 0:
            total_gpus_latest = rec_total_gpus
            active_gpus_sum += rec_active_gpus
            active_samples += 1
            if rec_active_gpus == 0:
                idle_samples += 1

        for user, gpu_count in rec_users.items():
            if gpu_count < min_user_gpus:
                continue
            entry = stats.setdefault(user, {
                "samples": 0,
                "gpu_seconds": 0.0,
                "gpu_max": 0.0,
                "active_seconds": 0,
                "last_ts": 0
            })
            entry["samples"] += 1
            entry["gpu_seconds"] += float(gpu_count) * rec_interval
            entry["gpu_max"] = max(entry["gpu_max"], float(gpu_count))
            entry["active_seconds"] += rec_interval
            entry["last_ts"] = max(entry["last_ts"], int(ts))

    users = []
    for user, entry in stats.items():
        if entry["samples"] == 0:
            continue
        users.append({
            "user": user,
            "active_hours": round(entry["active_seconds"] / 3600, 1),
            "gpu_hours": round(entry["gpu_seconds"] / 3600, 1),
            "max_gpus": int(entry["gpu_max"]),
            "samples": entry["samples"],
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["last_ts"]))
        })

    users.sort(key=lambda x: (x["gpu_hours"], x["active_hours"]), reverse=True)

    idle_rate = round((idle_samples / active_samples) * 100, 1) if active_samples else 0.0
    avg_active_gpus = round((active_gpus_sum / active_samples), 1) if active_samples else 0.0

    usage = {
        "window_days": history_days,
        "total_samples": total_samples,
        "sample_interval_sec": interval_sec,
        "total_gpus": total_gpus_latest,
        "idle_rate_percent": idle_rate,
        "avg_active_gpus": avg_active_gpus,
        "users": users
    }
    if cfg.get("daily_archive", True):
        daily_path, _ = build_daily_usage_archive(
            node_name=node_name,
            records=records,
            cfg=cfg,
            total_gpus_latest=total_gpus_latest,
            now_ts=now_ts
        )
        usage["daily_archive"] = os.path.basename(daily_path)
        usage["daily_archive_path"] = daily_path
    return usage

def main():
    if len(sys.argv) < 2:
        print("Usage: python agent.py <NodeName>")
        sys.exit(1)
        
    node_name = sys.argv[1]
    
    agent_cfg = load_agent_config()

    # 捕获整个流程，确保即使出错也能生成某种 JSON (可选)
    try:
        gpu_info = get_gpu_info()
        sys_info = get_system_info(agent_cfg.get("disk_mounts"))
        usage_info = build_usage_summary(node_name, gpu_info, agent_cfg)
    except Exception as e:
        print(f"Critical error: {e}")
        sys.exit(1)

    data = {
        "node": node_name,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "system": sys_info,
        "gpus": gpu_info
    }

    if usage_info is not None:
        data["usage"] = usage_info
    
    # 写入 JSON 文件
    filename = f"{node_name}.json"
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)
    
    print(f"Generated {filename}")

if __name__ == "__main__":
    main()
