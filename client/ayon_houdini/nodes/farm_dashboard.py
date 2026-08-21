#!/usr/bin/env python3
"""Dependency-free HTML and Discord dashboards for BMFX farm telemetry."""

from __future__ import annotations

import glob
import html
import math
import os
import re
import socket
import statistics
from datetime import datetime


CYAN = "#63c7f2"
BLUE = "#2586e7"
AMBER = "#efa91d"
PURPLE = "#a966eb"
GREEN = "#43d483"
RED = "#ff5d55"
MUTED = "#8e9aa6"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _duration(seconds):
    seconds = max(0, int(round(_number(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def _percentile(values, percentile):
    values = sorted(_number(value) for value in values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * float(percentile) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _frame_sort(value):
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group(0)) if match else 10 ** 12


def _safe(value):
    return html.escape(str(value if value not in (None, "") else "—"))


def _path_glob(template):
    value = str(template or "")
    value = re.sub(r"\$F\d*", "*", value)
    value = re.sub(r"#+", "*", value)
    value = value.replace("<STARTFRAME>", "*").replace("<ENDFRAME>", "*")
    return value


def _output_statistics(manifests):
    paths = []
    aovs = set()
    deep = False
    cryptomatte = False
    frame_range = []
    for manifest in manifests:
        paths.extend(glob.glob(_path_glob(manifest.get("output_path"))))
        aovs.update(manifest.get("aovs") or [])
        deep = deep or bool((manifest.get("deep_holdout") or {}).get("enabled"))
        cryptomatte = cryptomatte or bool(
            (manifest.get("cryptomatte") or {}).get("enabled")
            or manifest.get("render_kind") == "cryptomatte"
        )
        frame_range.extend(manifest.get("frame_range") or [])
    paths = list(dict.fromkeys(path for path in paths if os.path.isfile(path)))
    sizes = []
    for path in paths:
        try:
            sizes.append(os.path.getsize(path))
        except OSError:
            pass
    return {
        "aov_count": len(aovs),
        "deep": deep,
        "cryptomatte": cryptomatte,
        "output_format": "OpenEXR" if paths or manifests else "—",
        "frame_files": len(paths),
        "average_size": sum(sizes) / len(sizes) if sizes else 0,
        "total_size": sum(sizes),
    }


def _bytes(value):
    value = _number(value)
    if value <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    return "{:.1f} {}".format(value, units[index])


def _summary(request, layers):
    frames = []
    samples = []
    manifests = []
    for layer in layers:
        for frame in layer.get("frames") or []:
            item = dict(frame)
            item["layer"] = layer.get("name") or "Layer"
            frames.append(item)
        samples.extend(layer.get("timeline") or [])
        if layer.get("manifest"):
            manifests.append(layer["manifest"])
    frames.sort(key=lambda item: _frame_sort(item.get("frame")))
    times = [_number(item.get("render_seconds")) for item in frames]
    times = [value for value in times if value > 0]
    total = sum(_number(layer.get("total_seconds")) for layer in layers)
    if not total:
        total = sum(times)
    average = sum(times) / len(times) if times else 0.0
    median = statistics.median(times) if times else 0.0
    slow_threshold = max(_percentile(times, 95), median * 1.5)

    workers = {}
    for frame in frames:
        name = str(frame.get("worker") or "—")
        worker = workers.setdefault(name, {
            "name": name, "frames": 0, "seconds": [], "failures": 0,
            "retries": 0, "cpu": [], "ram": [], "gpu": [],
        })
        worker["frames"] += 1
        worker["seconds"].append(_number(frame.get("render_seconds")))
        worker["retries"] += int(_number(frame.get("retries")))
        status = str(frame.get("status") or "").lower()
        if status and not any(word in status for word in ("complete", "success")):
            worker["failures"] += 1
        for source, target in (
            ("average_cpu_percent", "cpu"),
            ("peak_ram_gb", "ram"),
            ("average_gpu_percent", "gpu"),
        ):
            value = _number(frame.get(source))
            if value:
                worker[target].append(value)
    worker_rows = []
    for worker in workers.values():
        seconds = [value for value in worker.pop("seconds") if value > 0]
        worker["average_seconds"] = sum(seconds) / len(seconds) if seconds else 0
        worker["average_cpu"] = (
            sum(worker["cpu"]) / len(worker["cpu"]) if worker["cpu"] else 0
        )
        worker["peak_ram"] = max(worker["ram"] or [0])
        worker["average_gpu"] = (
            sum(worker["gpu"]) / len(worker["gpu"]) if worker["gpu"] else 0
        )
        worker_rows.append(worker)
    worker_rows.sort(key=lambda item: item["average_seconds"])

    def sample_values(key):
        return [_number(item.get(key)) for item in samples]

    cpu = sample_values("cpu_percent")
    ram = sample_values("ram_gb")
    gpu = sample_values("gpu_percent")
    vram = sample_values("gpu_memory_gb")
    failures = sum(worker["failures"] for worker in worker_rows)
    retries = sum(worker["retries"] for worker in worker_rows)
    frame_numbers = [
        _frame_sort(item.get("frame")) for item in frames
        if _frame_sort(item.get("frame")) < 10 ** 12
    ]
    software = (manifests[0].get("software_versions") or {}) if manifests else {}
    render_metadata = (
        (manifests[0].get("render_metadata") or {}) if manifests else {}
    )
    output = _output_statistics(manifests)
    submitted = manifests[0].get("submitted_at_utc") if manifests else ""
    return {
        "frames": frames,
        "samples": samples,
        "manifests": manifests,
        "workers": worker_rows,
        "times": times,
        "total": total,
        "average": average,
        "median": median,
        "p90": _percentile(times, 90),
        "p95": _percentile(times, 95),
        "p99": _percentile(times, 99),
        "slow_threshold": slow_threshold,
        "fastest": min(frames, key=lambda item: _number(item.get("render_seconds")), default={}),
        "slowest": max(frames, key=lambda item: _number(item.get("render_seconds")), default={}),
        "average_cpu": sum(cpu) / len(cpu) if cpu else 0,
        "peak_cpu": max(cpu or [0]),
        "average_ram": sum(ram) / len(ram) if ram else 0,
        "peak_ram": max(ram or [0]),
        "average_gpu": sum(gpu) / len(gpu) if gpu else 0,
        "peak_gpu": max(gpu or [0]),
        "average_vram": sum(vram) / len(vram) if vram else 0,
        "peak_vram": max(vram or [0]),
        "failures": failures,
        "retries": retries,
        "frame_start": min(frame_numbers) if frame_numbers else None,
        "frame_end": max(frame_numbers) if frame_numbers else None,
        "software": software,
        "render_metadata": render_metadata,
        "renderer": manifests[0].get("renderer", "—") if manifests else "—",
        "submitted": submitted,
        "output": output,
        "job_ids": [layer.get("job_id") for layer in layers if layer.get("job_id")],
    }


def _line_svg(items, key, color, suffix="", width=720, height=250,
              x_key=None, reference=None, tooltip=None):
    values = [_number(item.get(key)) for item in items]
    if not items or not any(values):
        return '<div class="empty">Telemetry unavailable</div>'
    maximum = max(values + ([reference] if reference else []) + [1.0]) * 1.08
    left, top, right, bottom = 48.0, 16.0, 18.0, 34.0
    plot_w = width - left - right
    plot_h = height - top - bottom
    count = max(1, len(items) - 1)
    points = []
    circles = []
    for index, item in enumerate(items):
        value = values[index]
        x = left + plot_w * index / count
        y = top + plot_h * (1.0 - value / maximum)
        points.append("{:.1f},{:.1f}".format(x, y))
        label = tooltip(item) if tooltip else "{}{}".format(round(value, 2), suffix)
        circles.append(
            '<circle cx="{:.1f}" cy="{:.1f}" r="5" fill="transparent">'
            '<title>{}</title></circle>'.format(x, y, _safe(label))
        )
    grid = "".join(
        '<line x1="{l}" y1="{y:.1f}" x2="{r}" y2="{y:.1f}"/>'
        '<text x="4" y="{ty:.1f}">{label}</text>'.format(
            l=left, r=width-right, y=top + plot_h * fraction,
            ty=top + plot_h * fraction + 4,
            label="{:.0f}{}".format(maximum * (1-fraction), suffix),
        ) for fraction in (0, .25, .5, .75, 1)
    )
    reference_line = ""
    if reference:
        ref_y = top + plot_h * (1.0 - reference / maximum)
        reference_line = (
            '<line class="reference" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'
            '<text class="ref-label" x="{:.1f}" y="{:.1f}">AVG {}</text>'
        ).format(left, ref_y, width-right, ref_y, width-right-5, ref_y-5,
                 _duration(reference) if suffix == "s" else "{:.1f}{}".format(reference, suffix))
    first = items[0].get(x_key) if x_key else "0%"
    last = items[-1].get(x_key) if x_key else "100%"
    return (
        '<svg class="chart" viewBox="0 0 {w} {h}"><g class="grid">{grid}</g>'
        '{reference}<polyline class="series" style="stroke:{color}" points="{points}"/>'
        '{circles}<text class="axis" x="{left}" y="{bottom}">{first}</text>'
        '<text class="axis end" x="{right}" y="{bottom}">{last}</text></svg>'
    ).format(
        w=width, h=height, grid=grid, reference=reference_line, color=color,
        points=" ".join(points), circles="".join(circles), left=left,
        right=width-right, bottom=height-7, first=_safe(first), last=_safe(last),
    )


def _frame_tooltip(item):
    rows = [
        "Frame {}".format(item.get("frame", "—")),
        "Render Time: {}".format(_duration(item.get("render_seconds"))),
        "Worker: {}".format(item.get("worker") or "—"),
    ]
    for key, label, pattern in (
        ("peak_ram_gb", "Peak RAM", "{:.1f} GB"),
        ("average_cpu_percent", "CPU Avg", "{:.1f}%"),
        ("average_gpu_percent", "GPU Avg", "{:.1f}%"),
        ("peak_gpu_memory_gb", "Peak VRAM", "{:.1f} GB"),
    ):
        value = _number(item.get(key))
        if value:
            rows.append("{}: {}".format(label, pattern.format(value)))
    return "\n".join(rows)


def _kpi(label, value, color=CYAN, detail=""):
    return (
        '<div class="kpi"><div class="label">{}</div>'
        '<div class="kpi-value" style="color:{}">{}</div>'
        '<div class="detail">{}</div></div>'
    ).format(_safe(label), color, _safe(value), _safe(detail) if detail else "")


def _panel(title, body, classes=""):
    return '<section class="panel {}"><h2>{}</h2>{}</section>'.format(
        classes, _safe(title), body
    )


def _layer_table(layers):
    records = []
    for layer in layers:
        times = [_number(item.get("render_seconds")) for item in layer.get("frames") or []]
        times = [value for value in times if value > 0]
        average = sum(times) / len(times) if times else _number(layer.get("average_seconds"))
        records.append({
            "name": layer.get("name") or "Layer", "average": average,
            "minimum": min(times or [0]), "maximum": max(times or [0]),
            "p95": _percentile(times, 95), "frames": len(times),
        })
    records.sort(key=lambda item: item["average"], reverse=True)
    maximum = max([item["average"] for item in records] or [1])
    rows = []
    for item in records:
        width = 100 * item["average"] / maximum if maximum else 0
        rows.append(
            '<tr><td>{name}</td><td class="bar-cell"><span style="width:{width:.1f}%"></span></td>'
            '<td>{avg}</td><td>{minimum}</td><td>{maximum}</td><td>{p95}</td><td>{frames}</td></tr>'.format(
                name=_safe(item["name"]), width=width, avg=_duration(item["average"]),
                minimum=_duration(item["minimum"]), maximum=_duration(item["maximum"]),
                p95=_duration(item["p95"]), frames=item["frames"],
            )
        )
    return '<table><thead><tr><th>Layer</th><th></th><th>Avg</th><th>Min</th><th>Max</th><th>P95</th><th>Frames</th></tr></thead><tbody>{}</tbody></table>'.format("".join(rows))


def _worker_table(workers):
    if not workers:
        return '<div class="empty">Worker statistics unavailable</div>'
    rows = "".join(
        '<tr><td>{name}</td><td>{frames}</td><td>{avg}</td><td>{cpu}</td>'
        '<td>{ram}</td><td>{gpu}</td><td>{failures}</td><td>{retries}</td></tr>'.format(
            name=_safe(item["name"]), frames=item["frames"],
            avg=_duration(item["average_seconds"]),
            cpu="{:.1f}%".format(item["average_cpu"]) if item["average_cpu"] else "—",
            ram="{:.1f} GB".format(item["peak_ram"]) if item["peak_ram"] else "—",
            gpu="{:.1f}%".format(item["average_gpu"]) if item["average_gpu"] else "—",
            failures=item["failures"], retries=item["retries"],
        ) for item in workers
    )
    return '<table><thead><tr><th>Worker</th><th>Frames</th><th>Avg Frame</th><th>CPU Avg</th><th>RAM Peak</th><th>GPU Avg</th><th>Failures</th><th>Retries</th></tr></thead><tbody>{}</tbody></table>'.format(rows)


def _slow_table(summary):
    records = sorted(
        summary["frames"], key=lambda item: _number(item.get("render_seconds")),
        reverse=True,
    )[:10]
    rows = []
    for item in records:
        seconds = _number(item.get("render_seconds"))
        status = "Slow" if seconds >= summary["slow_threshold"] else "Warning"
        rows.append(
            '<tr><td>F{frame}</td><td>{layer}</td><td>{time}</td><td>{worker}</td>'
            '<td>{ram}</td><td>{cpu}</td><td>{gpu}</td><td><span class="badge {kind}">{status}</span></td></tr>'.format(
                frame=_safe(item.get("frame")), layer=_safe(item.get("layer")),
                time=_duration(seconds), worker=_safe(item.get("worker")),
                ram="{:.1f} GB".format(_number(item.get("peak_ram_gb"))) if item.get("peak_ram_gb") else "—",
                cpu="{:.1f}%".format(_number(item.get("average_cpu_percent"))) if item.get("average_cpu_percent") else "—",
                gpu="{:.1f}%".format(_number(item.get("average_gpu_percent"))) if item.get("average_gpu_percent") else "—",
                kind="error" if status == "Slow" else "warn", status=status,
            )
        )
    return '<table><thead><tr><th>Frame</th><th>Layer</th><th>Render Time</th><th>Worker</th><th>Peak RAM</th><th>CPU Avg</th><th>GPU Avg</th><th>Status</th></tr></thead><tbody>{}</tbody></table>'.format("".join(rows))


def _histogram(times):
    if not times:
        return '<div class="empty">Frame timing unavailable</div>'
    step = max(30, int(math.ceil(max(times) / 5.0 / 30.0) * 30))
    counts = [0] * 5
    for value in times:
        counts[min(4, int(value // step))] += 1
    maximum = max(counts or [1])
    bars = []
    for index, count in enumerate(counts):
        label = "{}+".format(step * index) if index == 4 else "{}–{}".format(step*index, step*(index+1))
        bars.append(
            '<div class="hist-col"><div class="hist-value">{}</div><div class="hist-bar" style="height:{:.1f}%"></div><div class="hist-label">{}s</div></div>'.format(
                count, 100 * count / maximum if maximum else 0, label
            )
        )
    return '<div class="histogram">{}</div>'.format("".join(bars))


def build_html(request, layers):
    data = _summary(request, layers)
    fastest = data["fastest"]
    slowest = data["slowest"]
    frame_range = (
        "{}–{}".format(data["frame_start"], data["frame_end"])
        if data["frame_start"] is not None else "—"
    )
    generated = datetime.now().astimezone().strftime("%b %d, %Y %I:%M %p")
    submitted = data["submitted"] or "—"
    if submitted != "—":
        try:
            submitted = datetime.fromisoformat(submitted.replace("Z", "+00:00")).astimezone().strftime("%b %d, %Y %I:%M %p")
        except ValueError:
            pass
    title = request.get("title") or "Houdini Render Farm Report"
    if title == "BMFX Render Farm Report":
        title = "Houdini Render Farm Report"

    kpis = "".join((
        _kpi("Total Render Time", _duration(data["total"]), CYAN, "{:,.0f} sec".format(data["total"])),
        _kpi("Average / Frame", _duration(data["average"]), CYAN, "{:.0f} sec".format(data["average"])),
        _kpi("Fastest Frame", _duration(fastest.get("render_seconds")), CYAN, "F{}".format(fastest.get("frame", "—"))),
        _kpi("Slowest Frame", _duration(slowest.get("render_seconds")), RED, "F{}".format(slowest.get("frame", "—"))),
        _kpi("Frames Rendered", len(data["frames"]), CYAN, frame_range),
        _kpi("Failed Frames", data["failures"], RED if data["failures"] else GREEN),
        _kpi("Retries", data["retries"], AMBER),
        _kpi("Workers", len(data["workers"]), "#d7e0e7"),
    ))
    resources = "".join((
        _kpi("Peak RAM", "{:.1f} GB".format(data["peak_ram"]) if data["peak_ram"] else "—", AMBER),
        _kpi("Average RAM", "{:.1f} GB".format(data["average_ram"]) if data["average_ram"] else "—", AMBER),
        _kpi("Peak CPU", "{:.1f}%".format(data["peak_cpu"]) if data["peak_cpu"] else "—", RED),
        _kpi("Average CPU", "{:.1f}%".format(data["average_cpu"]) if data["average_cpu"] else "—", CYAN),
        _kpi("Peak GPU", "{:.1f}%".format(data["peak_gpu"]) if data["peak_gpu"] else "—", PURPLE),
        _kpi("Average GPU", "{:.1f}%".format(data["average_gpu"]) if data["average_gpu"] else "—", PURPLE),
        _kpi("Peak VRAM", "{:.1f} GB".format(data["peak_vram"]) if data["peak_vram"] else "—", PURPLE),
        _kpi("Average VRAM", "{:.1f} GB".format(data["average_vram"]) if data["average_vram"] else "—", PURPLE),
    ))

    frame_chart = _line_svg(
        data["frames"], "render_seconds", CYAN, "s", width=1080, height=310,
        x_key="frame", reference=data["average"], tooltip=_frame_tooltip,
    )
    timeline = data["samples"]
    charts = [
        _panel("CPU Usage Over Time", _line_svg(timeline, "cpu_percent", BLUE, "%")),
        _panel("RAM Usage Over Time", _line_svg(timeline, "ram_gb", AMBER, " GB")),
    ]
    if data["peak_gpu"]:
        charts.append(_panel("GPU Utilization Over Time", _line_svg(timeline, "gpu_percent", PURPLE, "%")))
        charts.append(_panel("VRAM Usage Over Time", _line_svg(timeline, "gpu_memory_gb", "#4ed6b1", " GB")))
    else:
        charts.append(_panel("GPU / VRAM", '<div class="empty">GPU rendering not active or telemetry unavailable</div>', "span-2"))

    io_keys = ("disk_read_mb_s", "disk_write_mb_s", "network_read_mb_s", "network_write_mb_s")
    if any(any(_number(sample.get(key)) for sample in timeline) for key in io_keys):
        io_body = '<div class="io-grid">' + "".join(
            '<div><h3>{}</h3>{}</div>'.format(label, _line_svg(timeline, key, color, " MB/s", width=520, height=210))
            for key, label, color in (
                ("disk_read_mb_s", "Disk Read", BLUE),
                ("disk_write_mb_s", "Disk Write", AMBER),
                ("network_read_mb_s", "Worker Network Read", PURPLE),
                ("network_write_mb_s", "Worker Network Write", GREEN),
            )
        ) + "</div>"
        charts.append(_panel("Disk / Network I/O", io_body, "span-2"))

    memory_frames = [item for item in data["frames"] if _number(item.get("peak_ram_gb"))]
    details = []
    if memory_frames:
        details.append(_panel(
            "Peak Memory per Frame",
            _line_svg(memory_frames, "peak_ram_gb", AMBER, " GB", width=1080, height=260, x_key="frame"),
            "span-2",
        ))

    output = data["output"]
    software = data["software"]
    resolution_value = data["render_metadata"].get("resolution") or []
    resolution = (
        "{} × {}".format(resolution_value[0], resolution_value[1])
        if len(resolution_value) >= 2 else "—"
    )
    info_rows = (
        ("Renderer", "{} {}".format(data["renderer"], software.get("renderman") or "").strip()),
        ("Houdini", software.get("houdini") or "—"),
        ("Resolution", resolution),
        ("Frame Range", frame_range),
        ("AOV Count", output["aov_count"] or "—"),
        ("Cryptomatte", "Yes" if output["cryptomatte"] else "No"),
        ("Deep", "Yes" if output["deep"] else "No"),
        ("Output Format", output["output_format"]),
        ("Output Size", _bytes(output["total_size"])),
        ("Average Frame Size", _bytes(output["average_size"])),
    )
    output_table = '<div class="info-list">{}</div>'.format("".join(
        '<div><span>{}</span><strong>{}</strong></div>'.format(_safe(label), _safe(value))
        for label, value in info_rows
    ))
    health = '<div class="health">{}</div>'.format("".join((
        '<div class="success"><strong>{}</strong><span>Successful Frames</span></div>'.format(max(0, len(data["frames"])-data["failures"])),
        '<div class="error-text"><strong>{}</strong><span>Failed Frames</span></div>'.format(data["failures"]),
        '<div class="warning"><strong>{}</strong><span>Retries</span></div>'.format(data["retries"]),
        '<div><strong>{}</strong><span>Workers</span></div>'.format(len(data["workers"])),
    )))

    css = """
    :root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#0d1723 0,#070c12 38%,#05090e 100%);color:#edf2f6;font-family:Inter,Segoe UI,Arial,sans-serif;font-size:13px}.wrap{max-width:1580px;margin:auto;padding:25px}.header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}.eyebrow,.muted{color:#9aa7b3}.eyebrow{margin-bottom:8px}h1{font-size:30px;margin:0 0 8px}.status{color:#48df8c}.header-meta{display:grid;grid-template-columns:repeat(3,minmax(120px,1fr));gap:25px}.header-meta span{display:block;color:#8e9aa6;font-size:11px;text-transform:uppercase;margin-bottom:6px}.header-meta strong{font-size:12px}.kpi-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin:22px 0 12px}.resource-grid{display:grid;grid-template-columns:repeat(8,1fr);margin-bottom:14px}.resource-grid .kpi{border-radius:0;border-right:0}.resource-grid .kpi:first-child{border-radius:8px 0 0 8px}.resource-grid .kpi:last-child{border-radius:0 8px 8px 0;border-right:1px solid #34404d}.kpi,.panel{background:linear-gradient(145deg,#121a23,#0d141c);border:1px solid #34404d;border-radius:8px;box-shadow:0 9px 25px #0005}.kpi{min-height:91px;padding:15px 18px}.label{color:#9aa6b2;font-size:10px;font-weight:700;text-transform:uppercase}.kpi-value{font-size:22px;font-weight:750;margin-top:9px}.detail{font-size:11px;color:#c2ccd4;margin-top:5px}.dashboard{display:grid;grid-template-columns:1fr 1fr;gap:12px}.panel{padding:16px;min-width:0}.span-2{grid-column:1/-1}.hero{min-height:365px}h2{font-size:14px;text-transform:uppercase;margin:0 0 14px}h3{font-size:12px;color:#b9c4cd;margin:0 0 4px}.chart{width:100%;height:auto;overflow:visible}.chart .grid line{stroke:#26323e;stroke-width:1}.chart text{fill:#82909d;font-size:10px}.chart .series{fill:none;stroke-width:2}.chart .reference{stroke:#f2f5f7;stroke-width:1;stroke-dasharray:7 7;opacity:.65}.chart .ref-label{text-anchor:end;fill:#cbd3da}.axis.end{text-anchor:end}.empty{height:170px;display:flex;align-items:center;justify-content:center;color:#71808d}.io-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.panel-row{display:grid;grid-template-columns:1.35fr 1fr;gap:12px}.histogram{height:215px;display:flex;align-items:flex-end;gap:14px;padding:20px}.hist-col{height:100%;flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}.hist-bar{width:70%;min-height:2px;background:linear-gradient(#a86aeb,#7142b5);border-radius:3px 3px 0 0}.hist-value,.hist-label{font-size:10px;color:#aab5be;margin:5px 0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid #26313b;white-space:nowrap}th{font-size:10px;text-transform:uppercase;color:#8e9aa6}.bar-cell{width:35%}.bar-cell:before{content:"";position:absolute}.bar-cell span{display:block;height:10px;background:linear-gradient(90deg,#227edc,#54c8ef);border-radius:4px}.badge{padding:3px 8px;border-radius:10px;font-size:10px}.badge.warn{color:#ffc65a;background:#5a421d}.badge.error{color:#ff8f89;background:#572523}.info-list{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}.info-list div{display:flex;justify-content:space-between;border-bottom:1px solid #28333e;padding:10px 2px}.info-list span{color:#95a2ae}.health{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.health div{padding:18px;background:#0a1118;border:1px solid #2c3742;border-radius:6px}.health strong{font-size:25px;display:block}.health span{color:#8997a4;font-size:11px}.success strong{color:#43d483}.error-text strong{color:#ff5d55}.warning strong{color:#efa91d}.footer{display:flex;justify-content:space-between;color:#74828f;margin:16px 2px 0;font-size:11px}@media(max-width:1100px){.kpi-grid,.resource-grid{grid-template-columns:repeat(4,1fr)}.resource-grid .kpi{border:1px solid #34404d;border-radius:8px}.header{display:block}.header-meta{margin-top:18px}}@media(max-width:760px){.wrap{padding:12px}.kpi-grid,.resource-grid,.dashboard,.panel-row,.io-grid{grid-template-columns:1fr 1fr}.span-2{grid-column:1/-1}.header-meta{grid-template-columns:1fr 1fr}.info-list{grid-template-columns:1fr}.health{grid-template-columns:1fr 1fr}.panel{overflow-x:auto}}@media(max-width:480px){.kpi-grid,.resource-grid,.dashboard,.panel-row,.io-grid{grid-template-columns:1fr}.span-2{grid-column:auto}}
    """
    html_body = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}</style></head><body><main class="wrap">'
        '<header class="header"><div><div class="eyebrow">{project} &nbsp;|&nbsp; {shot} &nbsp;|&nbsp; {task}</div><h1>{title}</h1><div class="status">● Render chain completed successfully</div></div>'
        '<div class="header-meta"><div><span>Job ID</span><strong>{job}</strong></div><div><span>Submitted</span><strong>{submitted}</strong></div><div><span>Generated</span><strong>{generated}</strong></div><div><span>Renderer</span><strong>{renderer}</strong></div><div><span>Houdini</span><strong>{houdini}</strong></div><div><span>Frames</span><strong>{frame_range}</strong></div><div><span>Resolution</span><strong>{resolution}</strong></div></div></header>'
        '<div class="kpi-grid">{kpis}</div><div class="resource-grid">{resources}</div><div class="dashboard">'
        '{hero}{distribution}{charts}{details}{layers}{workers}{slow}{health}{output}'
        '</div><footer class="footer"><span>Generated by Houdini Bot on {host}</span><span>BlackMirrorFX Render Analytics</span></footer></main></body></html>'
    ).format(
        title=_safe(title), css=css, project=_safe(request.get("project")),
        shot=_safe(request.get("shot")), task=_safe(request.get("task")),
        job=_safe(", ".join(data["job_ids"]) or "—"), submitted=_safe(submitted),
        generated=_safe(generated), renderer=_safe("{} {}".format(data["renderer"], data["software"].get("renderman") or "").strip()),
        houdini=_safe(data["software"].get("houdini") or "—"), frame_range=frame_range,
        resolution=_safe(resolution),
        kpis=kpis, resources=resources,
        hero=_panel("Render Time per Frame", frame_chart, "span-2 hero"),
        distribution=_panel(
            "Frame Time Distribution",
            _histogram(data["times"]) +
            '<div class="info-list"><div><span>Median</span><strong>{}</strong></div>'
            '<div><span>P90</span><strong>{}</strong></div>'
            '<div><span>P95</span><strong>{}</strong></div>'
            '<div><span>P99</span><strong>{}</strong></div></div>'.format(
                _duration(data["median"]), _duration(data["p90"]),
                _duration(data["p95"]), _duration(data["p99"]),
            ),
        ),
        charts="".join(charts), details="".join(details),
        layers=_panel("Average Render Time by Layer", _layer_table(layers), "span-2"),
        workers=_panel("Worker Performance", _worker_table(data["workers"]), "span-2"),
        slow=_panel("Slowest Frames", _slow_table(data), "span-2"),
        health=_panel("Job Health", health),
        output=_panel("Renderer / Output Summary", output_table),
        host=_safe(socket.gethostname()),
    )
    return html_body


def _svg_polyline(items, key, x, y, width, height, color):
    values = [_number(item.get(key)) for item in items]
    maximum = max(values or [1]) or 1
    count = max(1, len(values) - 1)
    points = " ".join(
        "{:.1f},{:.1f}".format(
            x + width * index / count,
            y + height * (1.0 - value / maximum),
        ) for index, value in enumerate(values)
    )
    grid = "".join(
        '<line x1="{x}" y1="{gy:.1f}" x2="{right}" y2="{gy:.1f}" class="grid"/>'.format(
            x=x, right=x+width, gy=y+height*fraction
        ) for fraction in (0, .25, .5, .75, 1)
    )
    return grid + '<polyline points="{}" fill="none" stroke="{}" stroke-width="2.5"/>'.format(points, color)


def build_discord_svg(request, layers):
    """Return a dense 1600x1000 dashboard image for Discord."""
    data = _summary(request, layers)
    project = _safe(request.get("project"))
    shot = _safe(request.get("shot"))
    task = _safe(request.get("task"))
    frames = data["frames"]
    samples = data["samples"][-220:]
    fastest = data["fastest"]
    slowest = data["slowest"]
    kpis = (
        ("TOTAL RENDER TIME", _duration(data["total"]), CYAN),
        ("AVERAGE / FRAME", _duration(data["average"]), CYAN),
        ("FASTEST FRAME", "F{}  {}".format(fastest.get("frame", "—"), _duration(fastest.get("render_seconds"))), CYAN),
        ("SLOWEST FRAME", "F{}  {}".format(slowest.get("frame", "—"), _duration(slowest.get("render_seconds"))), RED),
        ("TOTAL FRAMES", str(len(frames)), CYAN),
        ("FAILED FRAMES", str(data["failures"]), GREEN if not data["failures"] else RED),
        ("RETRIES", str(data["retries"]), AMBER),
        ("WORKERS", str(len(data["workers"])), "#dce3e9"),
    )
    cards = []
    card_w = 184
    for index, (label, value, color) in enumerate(kpis):
        x = 24 + index * (card_w + 10)
        cards.append(
            '<rect x="{x}" y="126" width="{w}" height="82" rx="7" class="panel"/>'
            '<text x="{tx}" y="151" class="label">{label}</text><text x="{tx}" y="184" class="value" fill="{color}">{value}</text>'.format(
                x=x, w=card_w, tx=x+14, label=_safe(label), value=_safe(value), color=color
            )
        )
    resources = (
        ("PEAK RAM", "{:.1f} GB".format(data["peak_ram"]) if data["peak_ram"] else "—", AMBER),
        ("AVG RAM", "{:.1f} GB".format(data["average_ram"]) if data["average_ram"] else "—", AMBER),
        ("PEAK CPU", "{:.0f}%".format(data["peak_cpu"]) if data["peak_cpu"] else "—", RED),
        ("AVG CPU", "{:.0f}%".format(data["average_cpu"]) if data["average_cpu"] else "—", CYAN),
        ("PEAK GPU", "{:.0f}%".format(data["peak_gpu"]) if data["peak_gpu"] else "—", PURPLE),
        ("AVG GPU", "{:.0f}%".format(data["average_gpu"]) if data["average_gpu"] else "—", PURPLE),
        ("PEAK VRAM", "{:.1f} GB".format(data["peak_vram"]) if data["peak_vram"] else "—", PURPLE),
        ("AVG VRAM", "{:.1f} GB".format(data["average_vram"]) if data["average_vram"] else "—", PURPLE),
    )
    resource_cards = []
    for index, (label, value, color) in enumerate(resources):
        x = 24 + index * (card_w + 10)
        resource_cards.append(
            '<rect x="{x}" y="220" width="{w}" height="62" class="panel"/>'
            '<text x="{tx}" y="243" class="label">{label}</text><text x="{tx}" y="269" class="small-value" fill="{color}">{value}</text>'.format(
                x=x, w=card_w, tx=x+14, label=label, value=value, color=color
            )
        )
    layer_rows = []
    layer_max = max([_number(layer.get("average_seconds")) for layer in layers] or [1]) or 1
    for index, layer in enumerate(sorted(layers, key=lambda item: _number(item.get("average_seconds")), reverse=True)[:6]):
        y = 785 + index * 29
        value = _number(layer.get("average_seconds"))
        layer_rows.append(
            '<text x="42" y="{y}" class="small">{name}</text><rect x="190" y="{by}" width="300" height="10" rx="5" fill="#101821"/>'
            '<rect x="190" y="{by}" width="{width:.1f}" height="10" rx="5" fill="{color}"/><text x="505" y="{y}" class="small" text-anchor="end">{time}</text>'.format(
                y=y, by=y-10, name=_safe(layer.get("name")), width=250*value/layer_max,
                color=CYAN, time=_duration(value),
            )
        )
    worker_rows = []
    for index, worker in enumerate(data["workers"][:6]):
        y = 785 + index * 29
        worker_rows.append(
            '<text x="570" y="{y}" class="small">{name}</text><text x="790" y="{y}" class="small">{frames} fr</text>'
            '<text x="875" y="{y}" class="small">{avg}</text><text x="990" y="{y}" class="small">RAM {ram}</text>'.format(
                y=y, name=_safe(worker["name"]), frames=worker["frames"],
                avg=_duration(worker["average_seconds"]),
                ram="{:.1f}G".format(worker["peak_ram"]) if worker["peak_ram"] else "—",
            )
        )
    return '''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000">
    <defs><radialGradient id="bg" cx="80%" cy="0%" r="100%"><stop offset="0" stop-color="#0d1926"/><stop offset=".45" stop-color="#07101a"/><stop offset="1" stop-color="#05090e"/></radialGradient></defs>
    <style>.title{{font:700 28px sans-serif;fill:#f2f5f7}}.heading{{font:700 14px sans-serif;fill:#e9eef2}}.label{{font:700 10px sans-serif;fill:#91a0ad}}.value{{font:700 20px sans-serif}}.small-value{{font:700 17px sans-serif}}.small{{font:12px sans-serif;fill:#cbd4dc}}.muted{{font:12px sans-serif;fill:#8997a4}}.panel{{fill:#101821;stroke:#34404d}}.grid{{stroke:#25313c;stroke-width:1}}</style>
    <rect width="1600" height="1000" fill="url(#bg)"/><text x="24" y="34" class="muted">{project}  |  {shot}  |  {task}</text><text x="24" y="69" class="title">Houdini Render Farm Report</text><text x="24" y="96" fill="{green}" font-family="sans-serif" font-size="13">● Render chain completed successfully</text>
    <text x="1110" y="46" class="label">RENDERER</text><text x="1110" y="66" class="small">{renderer}</text><text x="1335" y="46" class="label">FRAMES</text><text x="1335" y="66" class="small">{frame_range}</text>
    {cards}{resource_cards}
    <rect x="24" y="296" width="1018" height="405" rx="7" class="panel"/><text x="42" y="324" class="heading">RENDER TIME PER FRAME</text>{frame_chart}
    <rect x="1054" y="296" width="522" height="405" rx="7" class="panel"/><text x="1072" y="324" class="heading">RESOURCE USAGE</text><text x="1072" y="358" class="label">CPU</text>{cpu}<text x="1072" y="472" class="label">RAM</text>{ram}<text x="1072" y="586" class="label">GPU</text>{gpu}
    <rect x="24" y="720" width="522" height="224" rx="7" class="panel"/><text x="42" y="752" class="heading">AVERAGE RENDER TIME BY LAYER</text>{layers}
    <rect x="558" y="720" width="510" height="224" rx="7" class="panel"/><text x="576" y="752" class="heading">WORKER PERFORMANCE</text>{workers}
    <rect x="1080" y="720" width="496" height="224" rx="7" class="panel"/><text x="1098" y="752" class="heading">OUTPUT / HEALTH</text><text x="1098" y="790" class="label">OUTPUT FORMAT</text><text x="1250" y="790" class="small">{format}</text><text x="1098" y="825" class="label">AOVS</text><text x="1250" y="825" class="small">{aovs}</text><text x="1098" y="860" class="label">DEEP / CRYPTO</text><text x="1250" y="860" class="small">{deep} / {crypto}</text><text x="1098" y="895" class="label">OUTPUT SIZE</text><text x="1250" y="895" class="small">{size}</text><text x="1415" y="895" fill="{green}" font-family="sans-serif" font-size="13">{health}</text>
    <text x="24" y="980" class="muted">Generated by Houdini Bot on {host}</text></svg>'''.format(
        project=project, shot=shot, task=task, green=GREEN,
        renderer=_safe("{} {}".format(data["renderer"], data["software"].get("renderman") or "").strip()),
        frame_range="{}–{}".format(data["frame_start"], data["frame_end"]) if data["frame_start"] is not None else "—",
        cards="".join(cards), resource_cards="".join(resource_cards),
        frame_chart=_svg_polyline(frames, "render_seconds", 55, 350, 960, 320, CYAN),
        cpu=_svg_polyline(samples, "cpu_percent", 1080, 372, 465, 72, BLUE),
        ram=_svg_polyline(samples, "ram_gb", 1080, 486, 465, 72, AMBER),
        gpu=_svg_polyline(samples, "gpu_percent", 1080, 600, 465, 72, PURPLE),
        layers="".join(layer_rows), workers="".join(worker_rows),
        format=_safe(data["output"]["output_format"]), aovs=data["output"]["aov_count"] or "—",
        deep="Yes" if data["output"]["deep"] else "No",
        crypto="Yes" if data["output"]["cryptomatte"] else "No",
        size=_bytes(data["output"]["total_size"]),
        health="HEALTHY" if not data["failures"] else "{} FAILED".format(data["failures"]),
        host=_safe(socket.gethostname()),
    )
