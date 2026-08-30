#!/usr/bin/env python3
"""Deadline render telemetry, graph reporting, and Discord notification.

The module has no Houdini dependency. Submission code can import
``attach_report_to_job`` from Houdini, while Deadline executes this file as
the post-job script of an existing terminal job. Render wrappers may use
``ResourceSampler`` to record CPU time, peak RAM, and GPU utilization per task.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


# Deadline's post-job loader does not consistently add the script directory
# to sys.path. Keep this legacy callback self-contained for already-submitted
# jobs that still reference the Houdini addon path.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)


_TIME_RE = re.compile(
    r"(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)$"
)


def _seconds(value):
    value = str(value or "").strip()
    match = _TIME_RE.search(value)
    if match:
        days, hours, minutes, seconds = match.groups()
        return (
            float(days or 0) * 86400
            + float(hours) * 3600
            + float(minutes) * 60
            + float(seconds)
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _duration(seconds):
    seconds = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _frame_number(value):
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group(0)) if match else None


def _deadline_output(command, *arguments):
    executable = shutil.which(command) or command
    try:
        return subprocess.check_output(
            [executable, *[str(value) for value in arguments]],
            stderr=subprocess.STDOUT,
            timeout=60,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("Farm report query failed: {}".format(exc), flush=True)
        return ""


def _task_records(deadline_command, job_id):
    """Parse the stable INI-style task output with tolerant key matching."""
    output = _deadline_output(deadline_command, "-GetJobTasks", job_id, "true")
    records = []
    current = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            normalized_key = key.strip().lower()
            if normalized_key in ("taskid", "task_id") and current:
                records.append(current)
                current = {}
            current[normalized_key] = value.strip()
    if current:
        records.append(current)
    return records


def _record_value(record, *names):
    for name in names:
        value = record.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def _deadline_layer_stats(deadline_command, job_id):
    records = _task_records(deadline_command, job_id)
    times = []
    frames = []
    cpu_times = []
    peak_ram_values = []
    for record in records:
        render_time = _seconds(_record_value(
            record,
            "taskrendertime",
            "rendertime",
            "tasktime",
            "elapsedtime",
        ))
        if render_time <= 0:
            continue
        frame = _record_value(
            record, "taskframelist", "framelist", "taskframe", "frames"
        )
        times.append(render_time)
        frame_value = _frame_number(frame)
        worker = _record_value(
            record, "taskslave", "slavename", "workername", "machinename",
            "taskmachinename",
        )
        status = _record_value(record, "taskstatus", "status") or "Completed"
        retries = int(_number(_record_value(
            record, "taskretrycount", "retrycount", "retries", "errorcount"
        )))
        frames.append({
            "frame": frame_value if frame_value is not None else (frame or "—"),
            "render_seconds": render_time,
            "worker": worker or "—",
            "status": status,
            "retries": retries,
        })
        cpu_time = _seconds(_record_value(
            record, "taskcputime", "cputime", "totalcputime"
        ))
        if cpu_time:
            cpu_times.append(cpu_time)
        ram_value = _record_value(
            record, "taskpeakramusage", "peakramusage", "peakram"
        )
        if ram_value:
            match = re.search(r"([0-9.]+)\s*(tb|gb|mb|kb|b)?", ram_value, re.I)
            if match:
                number = float(match.group(1))
                unit = (match.group(2) or "b").lower()
                factors = {
                    "tb": 1024.0, "gb": 1.0, "mb": 1.0 / 1024.0,
                    "kb": 1.0 / (1024.0 ** 2), "b": 1.0 / (1024.0 ** 3),
                }
                peak_ram_values.append(number * factors[unit])
    if not times:
        average = _seconds(
            _deadline_output(deadline_command, "-GetJobTaskAverageTime", job_id)
        )
        if average:
            times = [average]
    return {
        "average_seconds": sum(times) / len(times) if times else 0.0,
        "total_seconds": sum(times),
        "task_count": len(times),
        "cpu_time_seconds": sum(cpu_times),
        "average_cpu_percent": 0.0,
        "peak_ram_gb": max(peak_ram_values or [0.0]),
        "average_ram_gb": 0.0,
        "average_gpu_percent": 0.0,
        "peak_cpu_percent": 0.0,
        "peak_gpu_percent": 0.0,
        "peak_gpu_memory_gb": 0.0,
        "average_gpu_memory_gb": 0.0,
        "frames": frames,
        "slow_frames": sorted(
            frames, key=lambda item: item["render_seconds"], reverse=True
        )[:10],
    }


def _read_telemetry(pattern):
    payloads = []
    for path in sorted(glob.glob(str(pattern or ""))):
        try:
            with open(path, "r") as stream:
                payload = json.load(stream)
            if isinstance(payload, dict):
                payloads.append(payload)
        except (OSError, ValueError):
            continue
    if not payloads:
        return {
            "cpu_time_seconds": 0.0,
            "average_cpu_percent": 0.0,
            "peak_ram_gb": 0.0,
            "average_ram_gb": 0.0,
            "average_gpu_percent": 0.0,
            "peak_cpu_percent": 0.0,
            "peak_gpu_percent": 0.0,
            "peak_gpu_memory_gb": 0.0,
            "average_gpu_memory_gb": 0.0,
            "peak_disk_read_mb_s": 0.0,
            "peak_disk_write_mb_s": 0.0,
            "peak_network_read_mb_s": 0.0,
            "peak_network_write_mb_s": 0.0,
            "timeline": [],
            "tasks": [],
        }
    timeline = []
    tasks = []
    for payload in payloads:
        samples = payload.get("samples") or []
        for sample in samples:
            sample.setdefault("frame", _frame_number(payload.get("frame_start")))
            sample.setdefault("worker", payload.get("host") or "—")
        timeline.extend(samples)

        def sample_values(name):
            return [_number(item.get(name)) for item in samples]

        tasks.append({
            "frame_start": _frame_number(payload.get("frame_start")),
            "frame_end": _frame_number(payload.get("frame_end")),
            "worker": payload.get("host") or "—",
            "duration_seconds": _number(payload.get("duration_seconds")),
            "average_cpu_percent": (
                sum(sample_values("cpu_percent")) / len(samples) if samples else 0.0
            ),
            "peak_cpu_percent": max(sample_values("cpu_percent") or [0.0]),
            "average_ram_gb": (
                sum(sample_values("ram_gb")) / len(samples) if samples else 0.0
            ),
            "peak_ram_gb": max(sample_values("ram_gb") or [0.0]),
            "average_gpu_percent": (
                sum(sample_values("gpu_percent")) / len(samples) if samples else 0.0
            ),
            "peak_gpu_percent": max(sample_values("gpu_percent") or [0.0]),
            "average_gpu_memory_gb": (
                sum(sample_values("gpu_memory_gb")) / len(samples)
                if samples else 0.0
            ),
            "peak_gpu_memory_gb": max(
                sample_values("gpu_memory_gb") or [0.0]
            ),
        })
    def values(name):
        return [float(item.get(name) or 0) for item in timeline]
    cpu = values("cpu_percent")
    ram = values("ram_gb")
    gpu = values("gpu_percent")
    gpu_memory = values("gpu_memory_gb")
    disk_read = values("disk_read_mb_s")
    disk_write = values("disk_write_mb_s")
    network_read = values("network_read_mb_s")
    network_write = values("network_write_mb_s")
    return {
        "cpu_time_seconds": sum(
            float(item.get("cpu_time_seconds") or 0) for item in payloads
        ),
        "average_cpu_percent": sum(cpu) / len(cpu) if cpu else 0.0,
        "peak_cpu_percent": max(cpu or [0.0]),
        "peak_ram_gb": max(ram or [0.0]),
        "average_ram_gb": sum(ram) / len(ram) if ram else 0.0,
        "average_gpu_percent": sum(gpu) / len(gpu) if gpu else 0.0,
        "peak_gpu_percent": max(gpu or [0.0]),
        "peak_gpu_memory_gb": max(gpu_memory or [0.0]),
        "average_gpu_memory_gb": (
            sum(gpu_memory) / len(gpu_memory) if gpu_memory else 0.0
        ),
        "peak_disk_read_mb_s": max(disk_read or [0.0]),
        "peak_disk_write_mb_s": max(disk_write or [0.0]),
        "peak_network_read_mb_s": max(network_read or [0.0]),
        "peak_network_write_mb_s": max(network_write or [0.0]),
        "timeline": timeline,
        "tasks": tasks,
    }


def _proc_children(pid):
    try:
        with open("/proc/{}/task/{}/children".format(pid, pid), "r") as stream:
            return [int(value) for value in stream.read().split()]
    except (OSError, ValueError):
        return []


def _process_tree(pid):
    found = []
    pending = [int(pid)]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.append(current)
        pending.extend(_proc_children(current))
    return found


def _process_usage(pid):
    clock_ticks = float(os.sysconf("SC_CLK_TCK"))
    page_size = float(os.sysconf("SC_PAGE_SIZE"))
    cpu_seconds = 0.0
    rss_bytes = 0.0
    for process_id in _process_tree(pid):
        try:
            with open("/proc/{}/stat".format(process_id), "r") as stream:
                fields = stream.read().split()
            cpu_seconds += (float(fields[13]) + float(fields[14])) / clock_ticks
            with open("/proc/{}/statm".format(process_id), "r") as stream:
                rss_bytes += float(stream.read().split()[1]) * page_size
        except (OSError, ValueError, IndexError):
            continue
    return cpu_seconds, rss_bytes / (1024.0 ** 3)


def _process_io(pid):
    """Return bytes read/written by the render process tree."""
    read_bytes = 0.0
    write_bytes = 0.0
    for process_id in _process_tree(pid):
        try:
            with open("/proc/{}/io".format(process_id), "r") as stream:
                values = {}
                for line in stream:
                    key, _separator, value = line.partition(":")
                    values[key.strip()] = _number(value.strip())
            read_bytes += values.get("read_bytes", 0.0)
            write_bytes += values.get("write_bytes", 0.0)
        except OSError:
            continue
    return read_bytes, write_bytes


def _network_io():
    """Return worker-wide network bytes; loopback traffic is excluded."""
    received = 0.0
    transmitted = 0.0
    try:
        with open("/proc/net/dev", "r") as stream:
            for line in stream:
                if ":" not in line:
                    continue
                interface, values = line.split(":", 1)
                if interface.strip() == "lo":
                    continue
                fields = values.split()
                received += _number(fields[0])
                transmitted += _number(fields[8])
    except (OSError, IndexError):
        pass
    return received, transmitted


def _gpu_usage():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return 0.0, 0.0
    try:
        output = subprocess.check_output([
            executable,
            "--query-gpu=utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ], stderr=subprocess.DEVNULL, timeout=5).decode(errors="replace")
        values = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 2:
                values.append((float(parts[0]), float(parts[1]) / 1024.0))
        if values:
            return (
                sum(item[0] for item in values) / len(values),
                sum(item[1] for item in values),
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 0.0, 0.0


class ResourceSampler:
    """Sample one farm process tree and persist task telemetry as JSON."""

    def __init__(self, pid, output_path, metadata=None, interval=2.0):
        self.pid = int(pid)
        self.output_path = str(output_path or "")
        self.metadata = dict(metadata or {})
        self.interval = max(0.5, float(interval))
        self.samples = []
        self._stop = threading.Event()
        self._thread = None
        self._started = time.time()
        self._last_wall = self._started
        self._last_cpu = 0.0
        self._last_disk_read, self._last_disk_write = _process_io(self.pid)
        self._last_network_read, self._last_network_write = _network_io()

    def start(self):
        if not self.output_path:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            now = time.time()
            cpu_time, ram_gb = _process_usage(self.pid)
            elapsed = max(0.001, now - self._last_wall)
            cpu_delta = max(0.0, cpu_time - self._last_cpu)
            cpu_percent = min(
                100.0,
                100.0 * cpu_delta / elapsed / max(1, os.cpu_count() or 1),
            )
            gpu_percent, gpu_memory_gb = _gpu_usage()
            disk_read, disk_write = _process_io(self.pid)
            network_read, network_write = _network_io()
            to_mb_s = 1.0 / elapsed / (1024.0 ** 2)
            self.samples.append({
                "elapsed_seconds": now - self._started,
                "cpu_percent": cpu_percent,
                "cpu_time_seconds": cpu_time,
                "ram_gb": ram_gb,
                "gpu_percent": gpu_percent,
                "gpu_memory_gb": gpu_memory_gb,
                "disk_read_mb_s": max(
                    0.0, disk_read - self._last_disk_read
                ) * to_mb_s,
                "disk_write_mb_s": max(
                    0.0, disk_write - self._last_disk_write
                ) * to_mb_s,
                # Linux does not expose per-process network counters without
                # extra agents/eBPF. These values are worker-wide and are
                # labelled as such in the dashboard.
                "network_read_mb_s": max(
                    0.0, network_read - self._last_network_read
                ) * to_mb_s,
                "network_write_mb_s": max(
                    0.0, network_write - self._last_network_write
                ) * to_mb_s,
            })
            self._last_wall = now
            self._last_cpu = cpu_time
            self._last_disk_read = disk_read
            self._last_disk_write = disk_write
            self._last_network_read = network_read
            self._last_network_write = network_write
            self._stop.wait(self.interval)

    def stop(self):
        if not self.output_path:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2.0)
        cpu_time, _ram = _process_usage(self.pid)
        payload = dict(self.metadata)
        payload.update({
            "host": socket.gethostname(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": max(0.0, time.time() - self._started),
            "cpu_time_seconds": max(cpu_time, self._last_cpu),
            "samples": self.samples,
        })
        directory = os.path.dirname(self.output_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.output_path + ".tmp.{}".format(os.getpid())
        with open(temporary, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
        os.replace(temporary, self.output_path)


def _bar(value, maximum, color):
    width = 0 if maximum <= 0 else min(100, 100.0 * value / maximum)
    return (
        '<div class="bar-track"><div class="bar" style="width:{:.1f}%;'
        'background:{}"></div></div>'
    ).format(width, color)


def _timeline_svg(layers):
    """Return a dependency-free CPU/RAM/GPU line graph for the HTML report."""
    samples = []
    for layer in layers:
        samples.extend(layer.get("timeline") or [])
    if not samples:
        return '<div class="empty-chart">Telemetry is unavailable for these jobs.</div>'
    samples = samples[-180:]
    peak_ram = max(
        [float(item.get("ram_gb") or 0) for item in samples] or [1.0]
    ) or 1.0
    width, height = 640.0, 210.0
    left, top, right, bottom = 34.0, 14.0, 12.0, 24.0
    plot_width = width - left - right
    plot_height = height - top - bottom
    count = max(1, len(samples) - 1)

    def points(key, scale=100.0):
        result = []
        for index, sample in enumerate(samples):
            x = left + plot_width * index / count
            value = min(scale, max(0.0, float(sample.get(key) or 0)))
            y = top + plot_height * (1.0 - value / scale)
            result.append("{:.1f},{:.1f}".format(x, y))
        return " ".join(result)

    grid = "".join(
        '<line x1="{left}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" '
        'stroke="#353c45" stroke-width="1"/>'.format(
            left=left, right=width-right,
            y=top + plot_height * fraction,
        ) for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    return """<svg viewBox="0 0 640 210" role="img" aria-label="Resource usage over time" style="width:100%;height:auto">
    {grid}<polyline points="{cpu}" fill="none" stroke="#55b7df" stroke-width="2.5"/>
    <polyline points="{ram}" fill="none" stroke="#dda33d" stroke-width="2.5"/>
    <polyline points="{gpu}" fill="none" stroke="#ae6ee8" stroke-width="2.5"/>
    <text x="4" y="20" fill="#8d99a5" font-size="10">100</text>
    <text x="15" y="190" fill="#8d99a5" font-size="10">0</text>
    </svg><div class="legend"><span class="cpu">● CPU</span><span class="ram">● RAM</span><span class="gpu">● GPU</span></div>""".format(
        grid=grid,
        cpu=points("cpu_percent"),
        ram=points("ram_gb", peak_ram),
        gpu=points("gpu_percent"),
    )


def _build_html(request, layers):
    total_time = sum(layer["total_seconds"] for layer in layers)
    tasks = sum(layer["task_count"] for layer in layers)
    average_frame = total_time / tasks if tasks else 0.0
    peak_ram = max((layer["peak_ram_gb"] for layer in layers), default=0.0)
    average_gpu = (
        sum(layer["average_gpu_percent"] for layer in layers) / len(layers)
        if layers else 0.0
    )
    max_time = max((layer["average_seconds"] for layer in layers), default=1.0)
    layer_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(layer["name"]),
            _bar(layer["average_seconds"], max_time, "#65b8e8"),
            html.escape(_duration(layer["average_seconds"])),
        ) for layer in layers
    )
    resource_rows = "".join(
        """<tr><td>{name}</td><td>{cpu}{cpu_value:.1f}%</td>
        <td>{ram}{ram_value:.1f} GB</td><td>{gpu}{gpu_value:.1f}%</td></tr>""".format(
            name=html.escape(layer["name"]),
            cpu=_bar(layer["average_cpu_percent"], 100, "#55b7df"),
            cpu_value=layer["average_cpu_percent"],
            ram=_bar(layer["average_ram_gb"], max(peak_ram, 1), "#dda33d"),
            ram_value=layer["average_ram_gb"],
            gpu=_bar(layer["average_gpu_percent"], 100, "#ae6ee8"),
            gpu_value=layer["average_gpu_percent"],
        ) for layer in layers
    )
    slow = []
    for layer in layers:
        for item in layer["slow_frames"]:
            slow.append(dict(item, layer=layer["name"], ram=layer["peak_ram_gb"]))
    slow = sorted(slow, key=lambda item: item["render_seconds"], reverse=True)[:8]
    slow_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{:.1f} GB</td></tr>".format(
            html.escape(str(item["frame"])), html.escape(item["layer"]),
            _duration(item["render_seconds"]), item["ram"],
        ) for item in slow
    ) or '<tr><td colspan="4">No per-frame task data was available.</td></tr>'
    timeline_chart = _timeline_svg(layers)
    title = html.escape(request.get("title") or "BMFX Render Farm Report")
    project = html.escape(request.get("project") or "—")
    shot = html.escape(request.get("shot") or "—")
    task = html.escape(request.get("task") or "—")
    return """<!doctype html><html><head><meta charset="utf-8"><style>
    body{{margin:0;background:#171a1f;color:#e8edf2;font-family:Arial,sans-serif}}
    .wrap{{max-width:1200px;margin:auto;padding:24px}} h1{{margin:0;font-size:28px}}
    .ok{{color:#45d190;margin:7px 0 22px}} .meta{{color:#9facb8;font-size:13px}}
    .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}
    .card,.panel{{background:#22262d;border:1px solid #414852;border-radius:7px;padding:16px}}
    .label{{color:#9facb8;font-size:12px;text-transform:uppercase}} .value{{font-size:25px;color:#68bde9;margin-top:7px}}
    .panels{{display:grid;grid-template-columns:1fr 1fr;gap:12px}} h2{{font-size:17px;margin:0 0 14px}}
    table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{padding:8px;border-bottom:1px solid #363c45;text-align:left}}
    th{{color:#aeb8c2}} .bar-track{{display:inline-block;width:72%;height:10px;background:#15181c;border-radius:3px;margin-right:8px;vertical-align:middle}}
    .bar{{height:10px;border-radius:3px}} .resource td{{vertical-align:middle}}
    .legend{{text-align:right;font-size:12px;margin-top:3px}} .legend span{{margin-left:15px}}
    .cpu{{color:#55b7df}} .ram{{color:#dda33d}} .gpu{{color:#ae6ee8}}
    .empty-chart{{height:170px;display:flex;align-items:center;justify-content:center;color:#7f8b96}}
    .footer{{margin-top:14px;color:#71808d;font-size:12px}} @media(max-width:800px){{.grid,.panels{{grid-template-columns:1fr}}}}
    </style></head><body><div class="wrap"><div class="meta">{project} &nbsp;|&nbsp; {shot} &nbsp;|&nbsp; {task}</div>
    <h1>{title}</h1><div class="ok">● Render chain completed successfully</div>
    <div class="grid"><div class="card"><div class="label">Total Render Time</div><div class="value">{total}</div></div>
    <div class="card"><div class="label">Average / Frame</div><div class="value">{average}</div></div>
    <div class="card"><div class="label">Peak RAM</div><div class="value" style="color:#e2aa47">{ram:.1f} GB</div></div>
    <div class="card"><div class="label">Average GPU</div><div class="value" style="color:#b77aeb">{gpu:.1f}%</div></div></div>
    <div class="panels"><div class="panel"><h2>Average Render Time by Layer</h2><table>{layer_rows}</table></div>
    <div class="panel"><h2>Resource Usage Over Time</h2>{timeline_chart}</div>
    <div class="panel" style="grid-column:1/-1"><h2>Average Resource Usage by Layer</h2><table class="resource"><tr><th>Layer</th><th>CPU</th><th>RAM</th><th>GPU</th></tr>{resource_rows}</table></div>
    <div class="panel" style="grid-column:1/-1"><h2>Slowest Frames / Alerts</h2><table><tr><th>Frame</th><th>Layer</th><th>Render Time</th><th>Peak RAM</th></tr>{slow_rows}</table></div></div>
    <div class="footer">CPU time: {cpu_time} &nbsp;•&nbsp; Generated by BMFX Farm Report on {host}</div></div></body></html>""".format(
        project=project, shot=shot, task=task, title=title,
        total=_duration(total_time), average=_duration(average_frame),
        ram=peak_ram, gpu=average_gpu, layer_rows=layer_rows,
        timeline_chart=timeline_chart, resource_rows=resource_rows,
        slow_rows=slow_rows,
        cpu_time=_duration(sum(layer["cpu_time_seconds"] for layer in layers)),
        host=html.escape(socket.gethostname()),
    )


def _build_discord_svg(request, layers):
    """Build a self-contained 16:9 dashboard for Discord image preview."""
    total_time = sum(layer["total_seconds"] for layer in layers)
    tasks = sum(layer["task_count"] for layer in layers)
    average_frame = total_time / tasks if tasks else 0.0
    peak_ram = max((layer["peak_ram_gb"] for layer in layers), default=0.0)
    average_gpu = (
        sum(layer["average_gpu_percent"] for layer in layers) / len(layers)
        if layers else 0.0
    )
    project = html.escape(str(request.get("project") or "—"))
    shot = html.escape(str(request.get("shot") or "—"))
    task = html.escape(str(request.get("task") or "—"))
    layer_max = max((layer["average_seconds"] for layer in layers), default=1.0)
    layer_bars = []
    for index, layer in enumerate(layers[:6]):
        y = 330 + index * 40
        width = 0 if not layer_max else 335 * layer["average_seconds"] / layer_max
        name = html.escape(str(layer["name"])[:34])
        layer_bars.append(
            '<text x="54" y="{y}" class="small">{name}</text>'
            '<rect x="220" y="{bar_y}" width="335" height="12" rx="6" fill="#15191f"/>'
            '<rect x="220" y="{bar_y}" width="{width:.1f}" height="12" rx="6" fill="#59b9e6"/>'
            '<text x="565" y="{y}" class="small" text-anchor="end">{time}</text>'.format(
                y=y, bar_y=y - 11, name=name, width=width,
                time=_duration(layer["average_seconds"]),
            )
        )

    samples = []
    for layer in layers:
        samples.extend(layer.get("timeline") or [])
    samples = samples[-140:]
    chart_x, chart_y, chart_w, chart_h = 650.0, 320.0, 490.0, 205.0
    ram_max = max([float(item.get("ram_gb") or 0) for item in samples] or [1.0]) or 1.0

    def graph_points(key, maximum):
        if not samples:
            return ""
        count = max(1, len(samples) - 1)
        points = []
        for index, sample in enumerate(samples):
            value = min(maximum, max(0.0, float(sample.get(key) or 0)))
            x = chart_x + chart_w * index / count
            y = chart_y + chart_h * (1.0 - value / maximum)
            points.append("{:.1f},{:.1f}".format(x, y))
        return " ".join(points)

    grid = "".join(
        '<line x1="{x}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#363d47"/>'.format(
            x=chart_x, right=chart_x + chart_w,
            y=chart_y + chart_h * fraction,
        ) for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    timeline = (
        '<polyline points="{}" fill="none" stroke="#55b7df" stroke-width="3"/>'
        '<polyline points="{}" fill="none" stroke="#dda33d" stroke-width="3"/>'
        '<polyline points="{}" fill="none" stroke="#ae6ee8" stroke-width="3"/>'
    ).format(
        graph_points("cpu_percent", 100.0),
        graph_points("ram_gb", ram_max),
        graph_points("gpu_percent", 100.0),
    ) if samples else '<text x="895" y="410" class="muted" text-anchor="middle">Telemetry unavailable</text>'

    return """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675">
    <style>.title{{font:700 30px sans-serif;fill:#f1f5f8}}.heading{{font:700 17px sans-serif;fill:#e9eef2}}
    .label{{font:700 12px sans-serif;fill:#8e9aa6}}.value{{font:700 25px sans-serif;fill:#62bde9}}
    .small{{font:13px sans-serif;fill:#dbe2e8}}.muted{{font:13px sans-serif;fill:#8e9aa6}}</style>
    <rect width="1200" height="675" fill="#171a1f"/><text x="42" y="45" class="muted">{project}  |  {shot}  |  {task}</text>
    <text x="42" y="86" class="title">Houdini Render Farm Report</text><text x="42" y="113" fill="#45d190" font-family="sans-serif" font-size="14">● Render chain completed successfully</text>
    <g><rect x="42" y="140" width="266" height="100" rx="8" fill="#242930" stroke="#414a55"/><text x="62" y="172" class="label">TOTAL RENDER TIME</text><text x="62" y="211" class="value">{total}</text></g>
    <g><rect x="325" y="140" width="266" height="100" rx="8" fill="#242930" stroke="#414a55"/><text x="345" y="172" class="label">AVERAGE / FRAME</text><text x="345" y="211" class="value">{average}</text></g>
    <g><rect x="608" y="140" width="266" height="100" rx="8" fill="#242930" stroke="#414a55"/><text x="628" y="172" class="label">PEAK RAM</text><text x="628" y="211" font-family="sans-serif" font-size="25" font-weight="700" fill="#dda33d">{ram:.1f} GB</text></g>
    <g><rect x="891" y="140" width="267" height="100" rx="8" fill="#242930" stroke="#414a55"/><text x="911" y="172" class="label">AVERAGE GPU</text><text x="911" y="211" font-family="sans-serif" font-size="25" font-weight="700" fill="#ae6ee8">{gpu:.1f}%</text></g>
    <rect x="42" y="265" width="550" height="320" rx="8" fill="#22262d" stroke="#414a55"/><text x="62" y="292" class="heading">Average Render Time by Layer</text>{layer_bars}
    <rect x="608" y="265" width="550" height="320" rx="8" fill="#22262d" stroke="#414a55"/><text x="628" y="292" class="heading">CPU / RAM / GPU Usage Over Time</text>{grid}{timeline}
    <circle cx="780" cy="558" r="5" fill="#55b7df"/><text x="791" y="563" class="small">CPU</text><circle cx="865" cy="558" r="5" fill="#dda33d"/><text x="876" y="563" class="small">RAM</text><circle cx="950" cy="558" r="5" fill="#ae6ee8"/><text x="961" y="563" class="small">GPU</text>
    <text x="42" y="630" class="muted">Generated by Houdini Bot on {host}</text></svg>""".format(
        project=project, shot=shot, task=task,
        total=_duration(total_time), average=_duration(average_frame),
        ram=peak_ram, gpu=average_gpu, layer_bars="".join(layer_bars),
        grid=grid, timeline=timeline, host=html.escape(socket.gethostname()),
    )


def _write_discord_graph(request, layers, report_root):
    svg_path = os.path.join(report_root, "farm_report.svg")
    png_path = os.path.join(report_root, "farm_report.png")
    try:
        from ayon_houdini.nodes import farm_dashboard
    except ImportError:
        import farm_dashboard
    with open(svg_path, "w") as stream:
        stream.write(farm_dashboard.build_discord_svg(request, layers))
    converter = shutil.which("rsvg-convert")
    if not converter and os.path.isfile("/usr/bin/rsvg-convert"):
        converter = "/usr/bin/rsvg-convert"
    if not converter:
        print("rsvg-convert is unavailable; Discord PNG graph was skipped.", flush=True)
        return ""
    try:
        subprocess.check_output(
            [converter, "-w", "1600", "-h", "1000", "-o", png_path, svg_path],
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "output", b"") or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        print("Discord graph conversion failed: {}".format(str(output).strip() or exc), flush=True)
        return ""
    return png_path


def _multipart_body(payload, attachments):
    boundary = "----BMFXDiscord{}".format(uuid.uuid4().hex)
    body = bytearray()

    def add(value):
        body.extend(value.encode("utf-8") if isinstance(value, str) else value)

    add("--{}\r\n".format(boundary))
    add('Content-Disposition: form-data; name="payload_json"\r\n')
    add("Content-Type: application/json\r\n\r\n")
    add(json.dumps(payload))
    add("\r\n")
    for index, (path, content_type) in enumerate(attachments):
        add("--{}\r\n".format(boundary))
        add('Content-Disposition: form-data; name="files[{index}]"; filename="{name}"\r\n'.format(
            index=index, name=os.path.basename(path)
        ))
        add("Content-Type: {}\r\n\r\n".format(content_type))
        with open(path, "rb") as stream:
            add(stream.read())
        add("\r\n")
    add("--{}--\r\n".format(boundary))
    return bytes(body), boundary


def _send_discord_dm(request, layers, html_path, graph_path):
    bot_token = os.getenv("BMFX_DISCORD_BOT_TOKEN", "").strip()
    user_id = (
        os.getenv("BMFX_DISCORD_USER_ID", "").strip()
        or str(request.get("discord_user_id") or "").strip()
    )
    if not bot_token or not user_id:
        print(
            "Discord DM skipped: BMFX_DISCORD_BOT_TOKEN and "
            "BMFX_DISCORD_USER_ID must be set on the Deadline Worker.",
            flush=True,
        )
        return False
    if not user_id.isdigit():
        print("Discord DM skipped: BMFX_DISCORD_USER_ID is invalid.", flush=True)
        return False
    total_time = sum(layer["total_seconds"] for layer in layers)
    tasks = sum(layer["task_count"] for layer in layers)
    average_frame = total_time / tasks if tasks else 0.0
    peak_ram = max((layer["peak_ram_gb"] for layer in layers), default=0.0)
    average_gpu = (
        sum(layer["average_gpu_percent"] for layer in layers) / len(layers)
        if layers else 0.0
    )
    fields = [
        {"name": "Total Render Time", "value": _duration(total_time), "inline": True},
        {"name": "Average / Frame", "value": _duration(average_frame), "inline": True},
        {"name": "Peak RAM", "value": "{:.1f} GB".format(peak_ram), "inline": True},
        {"name": "Average GPU", "value": "{:.1f}%".format(average_gpu), "inline": True},
        {"name": "Layers", "value": "\n".join(
            "• {} — {}".format(layer["name"], _duration(layer["average_seconds"]))
            for layer in layers[:12]
        ) or "No layer statistics", "inline": False},
    ]
    embed = {
        "title": "✅ Render Completed",
        "description": "**{}**  •  **{}**  •  **{}**".format(
            request.get("project") or "Project",
            request.get("shot") or "Shot",
            request.get("task") or "Task",
        ),
        "color": 4575632,
        "fields": fields,
        "footer": {"text": "Houdini Bot • {}".format(socket.gethostname())},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    attachments = []
    if graph_path and os.path.isfile(graph_path):
        embed["image"] = {"url": "attachment://farm_report.png"}
        attachments.append((graph_path, "image/png"))
    # Keep the detailed HTML report on disk only. Discord displays HTML file
    # contents as a large source-code preview instead of rendering the page.
    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
        "attachments": [
            {"id": index, "filename": os.path.basename(path)}
            for index, (path, _content_type) in enumerate(attachments)
        ],
    }
    api_base = os.getenv(
        "BMFX_DISCORD_API_BASE", "https://discord.com/api/v10"
    ).rstrip("/")
    headers = {
        "Authorization": "Bot {}".format(bot_token),
        "User-Agent": "BMFX-Houdini-Farm-Reporter/1.0",
    }
    dm_request = urllib.request.Request(
        api_base + "/users/@me/channels",
        data=json.dumps({"recipient_id": user_id}).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(dm_request, timeout=60) as response:
            dm_channel = json.loads(response.read().decode("utf-8"))
        channel_id = str(dm_channel.get("id") or "").strip()
        if not channel_id:
            raise RuntimeError("Discord returned no DM channel ID.")
        body, boundary = _multipart_body(payload, attachments)
        message_request = urllib.request.Request(
            api_base + "/channels/{}/messages".format(channel_id),
            data=body,
            headers={
                **headers,
                "Content-Type": "multipart/form-data; boundary={}".format(
                    boundary
                ),
            },
            method="POST",
        )
        with urllib.request.urlopen(message_request, timeout=60) as response:
            response.read()
        print("Discord personal message sent by Houdini Bot.", flush=True)
        return True
    except (
        OSError,
        RuntimeError,
        ValueError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ) as exc:
        details = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                details = exc.read().decode(errors="replace")
            except Exception:
                details = ""
        print(
            "Discord personal message failed: {}".format(details or exc),
            flush=True,
        )
        return False


def generate_report(request_path):
    with open(request_path, "r") as stream:
        request = json.load(stream)
    deadline_command = request.get("deadline_command") or "deadlinecommand"
    layers = []
    for definition in request.get("layers") or []:
        deadline = _deadline_layer_stats(deadline_command, definition.get("job_id"))
        telemetry = _read_telemetry(definition.get("telemetry_glob"))
        layer = dict(deadline)
        for key, value in telemetry.items():
            if key in ("timeline", "tasks") or value:
                layer[key] = value
        telemetry_by_frame = {
            item.get("frame_start"): item
            for item in telemetry.get("tasks") or []
            if item.get("frame_start") is not None
        }
        frames = layer.get("frames") or []
        if not frames:
            frames = [{
                "frame": item.get("frame_start", "—"),
                "render_seconds": item.get("duration_seconds", 0.0),
                "worker": item.get("worker") or "—",
                "status": "Completed",
                "retries": 0,
            } for item in telemetry.get("tasks") or []]
        for frame in frames:
            measured = telemetry_by_frame.get(_frame_number(frame.get("frame")))
            if measured:
                for key, value in measured.items():
                    if key not in ("frame_start", "frame_end", "duration_seconds"):
                        frame[key] = value
                if frame.get("worker") in (None, "", "—"):
                    frame["worker"] = measured.get("worker") or "—"
        layer["frames"] = frames
        layer["slow_frames"] = sorted(
            frames, key=lambda item: _number(item.get("render_seconds")), reverse=True
        )[:10]
        layer["name"] = definition.get("name") or definition.get("job_id") or "Layer"
        layer["job_id"] = definition.get("job_id") or ""
        manifest = {}
        manifest_path = definition.get("manifest_path") or ""
        if manifest_path:
            try:
                with open(manifest_path, "r") as stream:
                    manifest = json.load(stream) or {}
            except (OSError, ValueError):
                pass
        layer["manifest"] = manifest
        layers.append(layer)
    report_root = request.get("report_root") or os.path.dirname(request_path)
    os.makedirs(report_root, exist_ok=True)
    report_path = os.path.join(report_root, "farm_report.html")
    try:
        from ayon_houdini.nodes import farm_dashboard
    except ImportError:
        import farm_dashboard
    html_body = farm_dashboard.build_html(request, layers)
    with open(report_path, "w") as stream:
        stream.write(html_body)
    summary_path = os.path.join(report_root, "farm_report.json")
    with open(summary_path, "w") as stream:
        json.dump({"request": request, "layers": layers}, stream, indent=2)
    graph_path = _write_discord_graph(request, layers, report_root)
    notified = _send_discord_dm(request, layers, report_path, graph_path)
    print("Farm report: {}".format(report_path), flush=True)
    print("Discord personal message sent: {}".format(notified), flush=True)
    return report_path


def _write_report_request(
    deadline_command, report_root, layers, project="", shot="", task="",
):
    """Write the data contract consumed by the Deadline post-job script."""
    os.makedirs(report_root, exist_ok=True)
    request_path = os.path.join(report_root, "farm_report_request.json")
    request = {
        "title": "BMFX Render Farm Report",
        "project": project,
        "shot": shot,
        "task": task,
        # The recipient ID is not a credential. Keeping it in the request
        # also covers Deadline post-job processes that do not inherit the
        # submitted render process environment on every repository setup.
        "discord_user_id": os.getenv("BMFX_DISCORD_USER_ID", "").strip(),
        "report_root": report_root,
        "deadline_command": deadline_command,
        "layers": layers,
    }
    with open(request_path, "w") as stream:
        json.dump(request, stream, indent=2, sort_keys=True)
    return request_path


def _deadline_control(deadline_command, *arguments):
    executable = shutil.which(deadline_command) or deadline_command
    try:
        return subprocess.check_output(
            [executable, *[str(value) for value in arguments]],
            stderr=subprocess.STDOUT,
            timeout=60,
        ).decode(errors="replace").strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "output", b"") or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise RuntimeError(
            "Deadline report configuration failed: {}".format(
                str(output).strip() or str(exc)
            )
        )


def _job_dependencies(deadline_command, job_id):
    """Return the job's existing dependency IDs when Deadline exposes them."""
    try:
        output = _deadline_control(
            deadline_command, "-GetJobSetting", job_id, "JobDependencies"
        )
    except RuntimeError:
        return []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    value = lines[-1].split("=", 1)[-1].strip()
    if value.lower() in ("", "false", "none", "null"):
        return []
    return [
        item.strip()
        for item in value.split(",")
        if re.fullmatch(r"[A-Za-z0-9_-]+", item.strip())
    ]


def attach_report_to_job(
    deadline_command, host_job_id, dependency_job_ids, report_root, layers,
    project="", shot="", task="",
):
    """Attach graph generation/Discord to an existing terminal Deadline job."""
    host_job_id = str(host_job_id or "").strip()
    if not host_job_id:
        return ""
    request_path = _write_report_request(
        deadline_command=deadline_command,
        report_root=report_root,
        layers=layers,
        project=project,
        shot=shot,
        task=task,
    )
    dependencies = _job_dependencies(deadline_command, host_job_id)
    dependencies.extend(
        str(value).strip()
        for value in dependency_job_ids
        if value and str(value).strip() != host_job_id
    )
    dependencies = list(dict.fromkeys(item for item in dependencies if item))

    # Configure the hook before changing dependencies so even a very short
    # terminal job cannot complete without owning the report callback.
    _deadline_control(
        deadline_command,
        "-SetJobExtraInfoKeyValue",
        host_job_id,
        "BMFXFarmReportRequest",
        request_path,
    )
    _deadline_control(
        deadline_command,
        "-SetJobSetting",
        host_job_id,
        "PostJobScript",
        os.path.abspath(__file__),
    )
    if dependencies:
        _deadline_control(
            deadline_command,
            "-SetJobSetting",
            host_job_id,
            "JobDependencies",
            ",".join(dependencies),
        )
        # Deadline only re-evaluates newly edited dependency settings while
        # the job is pending. This does not create or requeue any tasks.
        _deadline_control(deadline_command, "-PendJob", host_job_id)
    return host_job_id


def __main__(deadline_plugin, job=None):
    """Deadline post-job entry point; support both repository API signatures.

    Deadline 10 Python.NET may call this with ``(plugin, job)`` or with a
    string as the second argument, while older integrations pass only the
    plugin. Resolve the Job object by capability instead of argument truthiness.
    """
    try:
        if not hasattr(job, "GetJobExtraInfoKeyValueWithDefault"):
            job = deadline_plugin.GetJob()
        request_path = job.GetJobExtraInfoKeyValueWithDefault(
            "BMFXFarmReportRequest", ""
        )
        if not request_path:
            deadline_plugin.LogWarning(
                "BMFX farm report request is missing; Discord was skipped."
            )
            return
        deadline_plugin.LogInfo(
            "Generating BMFX farm report from {}".format(request_path)
        )
        report_path = generate_report(request_path)
        deadline_plugin.LogInfo(
            "BMFX farm report completed: {}".format(report_path)
        )
    except Exception as exc:
        # A mail-server/reporting problem must not turn a successful render or
        # AYON publish into a failed Deadline job.
        try:
            deadline_plugin.LogWarning(
                "BMFX farm report failed: {}".format(exc)
            )
        except Exception:
            print("BMFX farm report failed: {}".format(exc), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request",
        default=(
            os.getenv("BMFX_FARM_REPORT_REQUEST")
            or os.path.join(os.getcwd(), "farm_report_request.json")
        ),
        help=(
            "Report request JSON. Defaults to farm_report_request.json in "
            "the current job directory."
        ),
    )
    args = parser.parse_args(argv)
    if not os.path.isfile(args.request):
        parser.error("farm report request was not found: {}".format(args.request))
    generate_report(args.request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
