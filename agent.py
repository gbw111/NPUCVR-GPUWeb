import json
import time
import sys
import subprocess
import os
import psutil
from datetime import datetime, timedelta

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

def _day_text(ts):
    return datetime.fromtimestamp(int(ts)).date().isoformat()

def _timestamp_text(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))

def _cutoff_day_text(now_ts, history_days):
    keep_days = max(int(history_days), 1)
    cutoff_day = datetime.fromtimestamp(int(now_ts)).date() - timedelta(days=keep_days - 1)
    return cutoff_day.isoformat()

def _read_json_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: failed to read json {path}: {e}")
        return None

def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, path)

def _empty_usage_state(node_name):
    return {
        "version": 2,
        "node": node_name,
        "last_ts": 0,
        "days": {}
    }

def _empty_usage_day(day_text, interval_sec):
    return {
        "date": day_text,
        "sample_interval_sec": int(interval_sec),
        "samples": 0,
        "gpu_samples": 0,
        "idle_samples": 0,
        "active_gpus_sum": 0.0,
        "total_gpus": 0,
        "users": {}
    }

def _load_usage_state(path, node_name):
    state = _read_json_file(path)
    if not isinstance(state, dict):
        return _empty_usage_state(node_name)

    if not isinstance(state.get("days"), dict):
        state["days"] = {}

    try:
        state["last_ts"] = int(state.get("last_ts", 0))
    except Exception:
        state["last_ts"] = 0

    state["version"] = 2
    state["node"] = node_name
    return state

def _prune_usage_state(state, now_ts, history_days):
    cutoff = _cutoff_day_text(now_ts, history_days)
    days = state.get("days", {})
    state["days"] = {
        day_text: day_data
        for day_text, day_data in days.items()
        if isinstance(day_data, dict) and day_text >= cutoff
    }

def _add_usage_sample(state, ts, users, active_gpus, total_gpus, interval_sec, min_user_gpus):
    try:
        ts = int(ts)
        interval_sec = int(interval_sec)
    except Exception:
        return

    if interval_sec <= 0:
        return

    try:
        active_gpus = int(active_gpus)
    except Exception:
        active_gpus = 0

    try:
        total_gpus = int(total_gpus)
    except Exception:
        total_gpus = 0

    day_text = _day_text(ts)
    days = state.setdefault("days", {})
    day = days.get(day_text)
    if not isinstance(day, dict):
        day = _empty_usage_day(day_text, interval_sec)
        days[day_text] = day

    day["sample_interval_sec"] = interval_sec
    day["samples"] = int(day.get("samples", 0)) + 1
    if total_gpus > 0:
        day["gpu_samples"] = int(day.get("gpu_samples", 0)) + 1
        day["active_gpus_sum"] = float(day.get("active_gpus_sum", 0.0)) + active_gpus
        day["total_gpus"] = total_gpus
        if active_gpus == 0:
            day["idle_samples"] = int(day.get("idle_samples", 0)) + 1

    if not isinstance(users, dict):
        users = {}

    day_users = day.setdefault("users", {})
    for user, gpu_count in users.items():
        try:
            gpu_count = float(gpu_count)
        except Exception:
            continue
        if gpu_count < min_user_gpus:
            continue

        entry = day_users.setdefault(user or "unknown", {
            "samples": 0,
            "gpu_seconds": 0.0,
            "active_seconds": 0,
            "max_gpus": 0.0,
            "last_ts": 0
        })
        entry["samples"] = int(entry.get("samples", 0)) + 1
        entry["gpu_seconds"] = float(entry.get("gpu_seconds", 0.0)) + gpu_count * interval_sec
        entry["active_seconds"] = int(entry.get("active_seconds", 0)) + interval_sec
        entry["max_gpus"] = max(float(entry.get("max_gpus", 0.0)), gpu_count)
        entry["last_ts"] = max(int(entry.get("last_ts", 0)), ts)

    state["last_ts"] = max(int(state.get("last_ts", 0)), ts)

def _migrate_legacy_history(state, legacy_path, since_ts, interval_sec, min_user_gpus):
    if state.get("last_ts", 0) > 0 or not os.path.exists(legacy_path):
        return False

    records = _load_history_lines(legacy_path, since_ts, metric="gpu_hours")
    for rec in records:
        _add_usage_sample(
            state=state,
            ts=rec.get("ts", 0),
            users=rec.get("users", {}),
            active_gpus=rec.get("active_gpus", 0),
            total_gpus=rec.get("total_gpus", 0),
            interval_sec=rec.get("interval_sec", interval_sec),
            min_user_gpus=min_user_gpus
        )
    return bool(records)

def _format_usage_users(user_stats, hour_digits):
    users = []
    if not isinstance(user_stats, dict):
        return users

    for user, entry in user_stats.items():
        if not isinstance(entry, dict):
            continue

        samples = int(entry.get("samples", 0))
        if samples == 0:
            continue

        last_ts = int(entry.get("last_ts", 0))
        users.append({
            "user": user,
            "active_hours": round(float(entry.get("active_seconds", 0)) / 3600, hour_digits),
            "gpu_hours": round(float(entry.get("gpu_seconds", 0.0)) / 3600, hour_digits),
            "max_gpus": int(float(entry.get("max_gpus", 0))),
            "samples": samples,
            "last_seen": _timestamp_text(last_ts) if last_ts else None
        })

    users.sort(key=lambda x: (x["gpu_hours"], x["active_hours"]), reverse=True)
    return users

def _aggregate_user_stats(days):
    stats = {}
    for day in days:
        for user, entry in (day.get("users") or {}).items():
            if not isinstance(entry, dict):
                continue
            dst = stats.setdefault(user, {
                "samples": 0,
                "gpu_seconds": 0.0,
                "active_seconds": 0,
                "max_gpus": 0.0,
                "last_ts": 0
            })
            dst["samples"] += int(entry.get("samples", 0))
            dst["gpu_seconds"] += float(entry.get("gpu_seconds", 0.0))
            dst["active_seconds"] += int(entry.get("active_seconds", 0))
            dst["max_gpus"] = max(dst["max_gpus"], float(entry.get("max_gpus", 0.0)))
            dst["last_ts"] = max(dst["last_ts"], int(entry.get("last_ts", 0)))
    return stats

def _build_daily_usage_data(node_name, day_text, day, interval_sec):
    gpu_samples = int(day.get("gpu_samples", 0))
    idle_samples = int(day.get("idle_samples", 0))
    active_gpus_sum = float(day.get("active_gpus_sum", 0.0))
    idle_rate = round((idle_samples / gpu_samples) * 100, 1) if gpu_samples else 0.0
    avg_active_gpus = round(active_gpus_sum / gpu_samples, 1) if gpu_samples else 0.0

    return {
        "node": node_name,
        "date": day_text,
        "sample_interval_sec": int(day.get("sample_interval_sec", interval_sec)),
        "total_samples": int(day.get("samples", 0)),
        "total_gpus": int(day.get("total_gpus", 0)),
        "idle_rate_percent": idle_rate,
        "avg_active_gpus": avg_active_gpus,
        "users": _format_usage_users(day.get("users", {}), 4)
    }

def _write_daily_usage_archive(node_name, day_text, day, cfg):
    interval_sec = int(cfg.get("sample_interval_sec", 60))
    daily_data = _build_daily_usage_data(node_name, day_text, day, interval_sec)
    daily_dir = os.path.join(SCRIPT_DIR, "history", "daily")
    daily_path = os.path.join(daily_dir, f"{node_name}_{day_text}.json")
    _write_json_atomic(daily_path, daily_data)
    return daily_path, daily_data

def build_daily_usage_archive(node_name, state, cfg, now_ts=None):
    """
    Generate compact per-day, per-user GPU usage archive for a single node.
    Output:
        history/daily/{node_name}_YYYY-MM-DD.json
    Metric:
        gpu_hours = sum(user_gpu_count_per_sample * sample_interval_sec) / 3600
    """
    if now_ts is None:
        now_ts = int(time.time())

    today = datetime.fromtimestamp(int(now_ts)).date()
    archive_days = [
        (today - timedelta(days=1)).isoformat(),
        today.isoformat()
    ]

    current_path = None
    current_data = None
    for day_text in archive_days:
        day = (state.get("days") or {}).get(day_text)
        if not isinstance(day, dict):
            continue
        daily_path, daily_data = _write_daily_usage_archive(node_name, day_text, day, cfg)
        if day_text == today.isoformat():
            current_path = daily_path
            current_data = daily_data

    if current_path is None:
        day_text = today.isoformat()
        day = _empty_usage_day(day_text, int(cfg.get("sample_interval_sec", 60)))
        current_path, current_data = _write_daily_usage_archive(node_name, day_text, day, cfg)

    return current_path, current_data

def build_usage_summary(node_name, gpus, cfg):
    history_days = int(cfg.get("history_days", 7))
    if history_days <= 0:
        return None

    history_dir = os.path.join(SCRIPT_DIR, "history")
    os.makedirs(history_dir, exist_ok=True)
    state_path = os.path.join(history_dir, f"{node_name}_usage_state.json")
    legacy_history_path = os.path.join(history_dir, f"{node_name}_usage.jsonl")

    now_ts = int(time.time())
    window_sec = history_days * 24 * 3600
    since_ts = now_ts - window_sec

    min_user_gpus = int(cfg.get("min_user_gpus", 1))
    exclude_users = set(cfg.get("exclude_users") or [])
    interval_sec = int(cfg.get("sample_interval_sec", 60))

    state = _load_usage_state(state_path, node_name)
    _migrate_legacy_history(
        state=state,
        legacy_path=legacy_history_path,
        since_ts=since_ts,
        interval_sec=interval_sec,
        min_user_gpus=min_user_gpus
    )
    _prune_usage_state(state, now_ts, history_days)

    # Keep real-time status in {node}.json; only the persisted usage history is compacted.
    snapshot_users, active_gpus, total_gpus = _collect_user_snapshot(gpus, exclude_users)
    _add_usage_sample(
        state=state,
        ts=now_ts,
        users=snapshot_users,
        active_gpus=active_gpus,
        total_gpus=total_gpus,
        interval_sec=interval_sec,
        min_user_gpus=min_user_gpus
    )
    _prune_usage_state(state, now_ts, history_days)
    _write_json_atomic(state_path, state)

    ordered_days = [
        day
        for _, day in sorted((state.get("days") or {}).items())
        if isinstance(day, dict)
    ]

    total_samples = sum(int(day.get("samples", 0)) for day in ordered_days)
    active_samples = sum(int(day.get("gpu_samples", 0)) for day in ordered_days)
    idle_samples = sum(int(day.get("idle_samples", 0)) for day in ordered_days)
    active_gpus_sum = sum(float(day.get("active_gpus_sum", 0.0)) for day in ordered_days)

    total_gpus_latest = total_gpus
    for day in ordered_days:
        day_total_gpus = int(day.get("total_gpus", 0))
        if day_total_gpus > 0:
            total_gpus_latest = day_total_gpus

    users = _format_usage_users(_aggregate_user_stats(ordered_days), 1)
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
            state=state,
            cfg=cfg,
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
