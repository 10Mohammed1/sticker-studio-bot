"""Admin 2.0 monitoring: memory, storage, queue and error counters.

Everything degrades gracefully: psutil is optional, /proc only exists on Linux,
and the Windows path uses ctypes so the panel works on a laptop, a VPS and
Railway without extra dependencies.
"""
from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from ..tasks.maintenance import directory_size

_STARTED = time.time()


def resident_mb() -> float | None:
    """Resident set size of this process in MB, or None when unavailable."""
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.GetCurrentProcess
            process.restype = wintypes.HANDLE
            query = kernel32.K32GetProcessMemoryInfo
            query.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            query.restype = wintypes.BOOL
            if query(process(), ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024 * 1024), 1)
        except Exception:
            return None
        return None
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def uptime_seconds() -> int:
    return int(time.time() - _STARTED)


def disk_snapshot(path: Path) -> dict[str, float]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total_gb": 0.0, "free_gb": 0.0, "used_gb": 0.0, "used_percent": 0.0}
    total = usage.total / (1024 ** 3)
    free = usage.free / (1024 ** 3)
    used = total - free
    return {
        "total_gb": round(total, 1),
        "free_gb": round(free, 1),
        "used_gb": round(used, 1),
        "used_percent": round((used / total) * 100, 1) if total else 0.0,
    }


async def collect(db: Any, queue: Any, data_dir: Path) -> dict[str, Any]:
    """Gather one monitoring snapshot without failing on a missing metric."""
    stats = await db.get_stats()
    queue_stats = queue.stats()
    active = queue.snapshot()
    try:
        db_size_mb = round(Path(db.path).stat().st_size / (1024 * 1024), 2)
    except (OSError, AttributeError):
        db_size_mb = 0.0
    return {
        "users": stats.users,
        "packs": stats.packs,
        "jobs": stats.jobs,
        "failed_jobs": stats.failed,
        "blocked": stats.blocked,
        "queue": {
            "workers": queue_stats.workers,
            "queued": queue_stats.queued,
            "running": queue_stats.running,
            "completed": queue_stats.completed,
            "failed": queue_stats.failed,
            "cancelled": queue_stats.cancelled,
        },
        "active_jobs": active[:8],
        "memory_mb": resident_mb(),
        "uptime_seconds": uptime_seconds(),
        "disk": disk_snapshot(data_dir),
        "data_dir_mb": round(directory_size(data_dir) / (1024 * 1024), 1),
        "database_mb": db_size_mb,
        "python": platform.python_version(),
        "platform": platform.system(),
    }


def format_report(snapshot: dict[str, Any]) -> str:
    """Arabic Telegram-HTML report built from collect()."""
    queue = snapshot.get("queue") or {}
    disk = snapshot.get("disk") or {}
    memory = snapshot.get("memory_mb")
    memory_text = f"{memory} MB" if memory is not None else "غير متاح"
    uptime = int(snapshot.get("uptime_seconds") or 0)
    minutes, seconds = divmod(uptime, 60)
    hours, minutes = divmod(minutes, 60)
    used_gb = disk.get("used_gb", 0)
    total_gb = disk.get("total_gb", 0)
    used_percent = disk.get("used_percent", 0)
    free_gb = disk.get("free_gb", 0)
    running = queue.get("running", 0)
    queued = queue.get("queued", 0)
    workers = queue.get("workers", 0)
    completed = queue.get("completed", 0)
    failed = queue.get("failed", 0)
    cancelled = queue.get("cancelled", 0)
    users = snapshot.get("users", 0)
    blocked = snapshot.get("blocked", 0)
    packs = snapshot.get("packs", 0)
    jobs = snapshot.get("jobs", 0)
    failed_jobs = snapshot.get("failed_jobs", 0)
    data_dir_mb = snapshot.get("data_dir_mb", 0)
    database_mb = snapshot.get("database_mb", 0)
    python_version = snapshot.get("python")
    platform_name = snapshot.get("platform")
    lines = [
        "🩺 <b>مراقبة Sticker Studio</b>",
        f"⏱ وقت التشغيل: {hours}س {minutes}د {seconds}ث",
        f"🧠 الذاكرة: {memory_text}",
        f"💾 القرص: {used_gb}/{total_gb} GB ({used_percent}%) — متاح {free_gb} GB",
        f"📁 البيانات: {data_dir_mb} MB — قاعدة البيانات: {database_mb} MB",
        "",
        f"👥 المستخدمون: {users} — 🚫 المحظورون: {blocked}",
        f"📦 الحزم: {packs} — ⚙️ العمليات: {jobs} — ⚠️ الفاشلة: {failed_jobs}",
        "",
        f"🧵 الطابور: {running} قيد التنفيذ، {queued} بالانتظار (عمال: {workers})",
        f"✅ مكتملة: {completed} — ❌ فاشلة: {failed} — 🚫 ملغاة: {cancelled}",
    ]
    active = snapshot.get("active_jobs") or []
    if active:
        lines.append("")
        lines.append("<b>المهام النشطة</b>")
        for job in active:
            task_id = str(job.get("task_id", ""))[:8]
            task_type = job.get("task_type", "")
            status = job.get("status", "")
            progress = job.get("progress", 0)
            elapsed = job.get("elapsed", 0)
            lines.append(
                f"• <code>{task_id}</code> {task_type} — {status} {progress}% ({elapsed}s)"
            )
    lines.append("")
    lines.append(f"🐍 Python {python_version} على {platform_name}")
    return "\n".join(lines)
