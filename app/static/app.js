const form = document.getElementById("upload-form");
const input = document.getElementById("file");
const drop = document.getElementById("drop");
const dropLabel = document.getElementById("drop-label");
const submit = document.getElementById("submit");
const results = document.getElementById("results");
const selected = document.getElementById("selected");
const selectedCount = document.getElementById("selected-count");
const fileList = document.getElementById("file-list");
const clearBtn = document.getElementById("clear-files");

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function currentFiles() {
  return input.files ? Array.from(input.files) : [];
}

function renderSelectedFiles() {
  const files = currentFiles();
  submit.disabled = files.length === 0;

  if (!files.length) {
    selected.hidden = true;
    fileList.innerHTML = "";
    selectedCount.textContent = "0 files selected";
    dropLabel.innerHTML = "Drop files here or <u>browse</u>";
    return;
  }

  selected.hidden = false;
  selectedCount.textContent =
    files.length === 1 ? "1 file selected" : `${files.length} files selected`;
  dropLabel.innerHTML =
    files.length === 1
      ? `<strong>1 file ready</strong> — drop more or <u>browse</u>`
      : `<strong>${files.length} files ready</strong> — drop more or <u>browse</u>`;

  fileList.innerHTML = files
    .map(
      (f, i) => `
      <li data-idx="${i}">
        <span class="file-idx">${i + 1}.</span>
        <span class="file-name" title="${f.name.replace(/"/g, "&quot;")}">${f.name}</span>
        <span class="file-size">${formatBytes(f.size)}</span>
        <span class="file-status" data-status="queued">queued</span>
      </li>`
    )
    .join("");
}

function setFileStatus(idx, status, detail) {
  const li = fileList.querySelector(`li[data-idx="${idx}"]`);
  if (!li) return;
  const el = li.querySelector(".file-status");
  if (!el) return;
  el.dataset.status = status;
  el.textContent = detail || status;
  li.dataset.status = status;
}

function clearFiles() {
  input.value = "";
  renderSelectedFiles();
  results.hidden = true;
  results.innerHTML = "";
  submit.textContent = "Process uploads";
}

input.addEventListener("change", renderSelectedFiles);
clearBtn.addEventListener("click", clearFiles);

["dragenter", "dragover"].forEach((ev) => {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("drag");
  });
});
["dragleave", "drop"].forEach((ev) => {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
  });
});
drop.addEventListener("drop", (e) => {
  const incoming = Array.from(e.dataTransfer.files || []).filter((f) =>
    /\.(xlsx|xlsm|xls)$/i.test(f.name)
  );
  if (!incoming.length) return;

  // Merge with already selected files (by name+size)
  const existing = currentFiles();
  const key = (f) => `${f.name}::${f.size}`;
  const map = new Map(existing.map((f) => [key(f), f]));
  incoming.forEach((f) => map.set(key(f), f));

  const dt = new DataTransfer();
  map.forEach((f) => dt.items.add(f));
  input.files = dt.files;
  renderSelectedFiles();
});

function errDetail(data, res) {
  if (!data) return res.statusText;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  }
  return data.error || res.statusText;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = currentFiles();
  if (!files.length) return;

  const total = files.length;
  results.hidden = false;
  results.innerHTML = `
    <div class="result batch-summary">
      <strong>Sending ${total} file${total === 1 ? "" : "s"}:</strong>
      <ol class="sent-list">
        ${files.map((f) => `<li>${f.name} <span class="muted">(${formatBytes(f.size)})</span></li>`).join("")}
      </ol>
    </div>`;
  submit.disabled = true;

  let ok = 0;
  let fail = 0;

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    submit.textContent = `Processing ${i + 1} / ${total}…`;
    setFileStatus(i, "uploading", "uploading…");

    const box = document.createElement("div");
    box.className = "result";
    box.textContent = `(${i + 1}/${total}) Uploading ${file.name}…`;
    results.appendChild(box);
    box.scrollIntoView({ block: "nearest" });

    try {
      const body = new FormData();
      body.append("file", file);
      const res = await fetch("/api/upload", {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      if (res.status === 401) {
        location.href = "/login";
        return;
      }
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        /* ignore */
      }
      if (!res.ok) throw new Error(errDetail(data, res));

      ok += 1;
      setFileStatus(
        i,
        "ok",
        `ok · ${data.flights_inserted} flights · ${data.boardings_inserted} pax`
      );
      box.classList.add("ok");
      box.innerHTML = `<strong>(${i + 1}/${total}) ${data.filename}</strong><br/>
        sheets/flights found <code>${data.flights_found}</code> ·
        inserted <code>${data.flights_inserted}</code> ·
        skipped ${data.flights_skipped} ·
        boardings <code>${data.boardings_inserted}</code>
        ${data.notes ? `<br/><span class="muted">${data.notes}</span>` : ""}`;
    } catch (err) {
      fail += 1;
      setFileStatus(i, "err", "failed");
      box.classList.add("err");
      box.textContent = `(${i + 1}/${total}) ${file.name}: ${err.message || err}`;
    }
  }

  submit.textContent = `Done — ${ok} ok, ${fail} failed of ${total}`;
  submit.disabled = false;

  await refreshUploadLog();
  setTimeout(() => location.reload(), 1500);
});

async function refreshUploadLog() {
  const root = document.getElementById("upload-log");
  if (!root) return;
  try {
    const res = await fetch("/api/uploads/recent?limit=30", {
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.href = "/login";
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    const uploads = data.uploads || [];
    if (!uploads.length) {
      root.innerHTML = `<p class="muted" id="upload-log-empty">Nenhum upload ainda.</p>`;
      return;
    }
    root.innerHTML = `<ul class="upload-log-list">${uploads
      .map(
        (u) => `
      <li>
        <div class="upload-when">
          <span class="upload-day">${u.day}</span>
          <span class="upload-time">${u.time}</span>
        </div>
        <div class="upload-body">
          <strong class="upload-file" title="${String(u.filename).replace(/"/g, "&quot;")}">${u.filename}</strong>
          <span class="upload-meta">
            ${u.flights_inserted} voos · ${u.boardings_inserted} pax${
              u.flights_skipped ? ` · ${u.flights_skipped} ignorados` : ""
            }
          </span>
        </div>
      </li>`
      )
      .join("")}</ul>`;
  } catch (_) {
    /* ignore refresh errors */
  }
}

function drawLineChart(canvas, points, opts) {
  const values = points.map((p) => p.y);
  const labels = points.map((p) => p.x);
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || 220;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const pad = { t: 16, r: 14, b: 28, l: 40 };
  const w = cssW - pad.l - pad.r;
  const h = cssH - pad.t - pad.b;
  ctx.clearRect(0, 0, cssW, cssH);

  if (!values.length) {
    ctx.fillStyle = "#9aa89a";
    ctx.font = "13px IBM Plex Sans, sans-serif";
    ctx.fillText("No data yet", pad.l, pad.t + 20);
    return;
  }

  const minV = opts.min != null ? opts.min : Math.min(...values, 0);
  const maxV = opts.max != null ? opts.max : Math.max(...values, 1);
  const span = maxV - minV || 1;

  ctx.strokeStyle = "#2c382f";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (h * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(pad.l + w, y);
    ctx.stroke();
    const val = maxV - (span * i) / 4;
    ctx.fillStyle = "#9aa89a";
    ctx.font = "11px IBM Plex Sans, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(
      opts.formatY ? opts.formatY(val) : String(Math.round(val)),
      pad.l - 6,
      y + 3
    );
  }

  const xAt = (i) =>
    pad.l + (values.length === 1 ? w / 2 : (w * i) / (values.length - 1));
  const yAt = (v) => pad.t + h - ((v - minV) / span) * h;

  if (opts.fill) {
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = xAt(i);
      const y = yAt(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xAt(values.length - 1), pad.t + h);
    ctx.lineTo(xAt(0), pad.t + h);
    ctx.closePath();
    ctx.fillStyle = opts.fill;
    ctx.fill();
  }

  ctx.strokeStyle = opts.color || "#c4a35a";
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = xAt(i);
    const y = yAt(v);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  values.forEach((v, i) => {
    ctx.beginPath();
    ctx.fillStyle = opts.color || "#c4a35a";
    ctx.arc(xAt(i), yAt(v), 3, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#9aa89a";
  ctx.font = "10px IBM Plex Sans, sans-serif";
  ctx.textAlign = "center";
  const step = values.length > 8 ? 2 : 1;
  labels.forEach((lab, i) => {
    if (i % step !== 0 && i !== labels.length - 1) return;
    const short = lab.length >= 7 ? lab.slice(2) : lab;
    ctx.fillText(short, xAt(i), cssH - 8);
  });
}

async function loadCustomerKpis() {
  const rangeEl = document.getElementById("kpi-range");
  const tbody = document.querySelector("#kpi-table tbody");
  if (!rangeEl || !tbody) return;

  try {
    const res = await fetch("/api/stats/customers-kpis?months=12", {
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.href = "/login";
      return;
    }
    if (!res.ok) throw new Error("KPI request failed");
    const data = await res.json();
    const s = data.summary || {};
    const monthly = data.monthly || [];

    if (!monthly.length) {
      rangeEl.textContent = "No boarding history yet";
      return;
    }

    rangeEl.textContent =
      data.data_start && data.data_end
        ? `History ${data.data_start} → ${data.data_end} · ${data.months_available} mo`
        : `${data.months_available} months`;

    document.getElementById("kpi-unique").textContent = String(
      s.unique_customers_ltm ?? "—"
    );
    document.getElementById("kpi-repeat").textContent =
      s.repeat_rate_pct != null ? `${s.repeat_rate_pct}%` : "—";
    document.getElementById("kpi-new").textContent = String(
      s.new_customers_ltm ?? "—"
    );
    document.getElementById("kpi-repeaters").textContent = String(
      s.repeat_customers_ltm ?? "—"
    );

    tbody.innerHTML = monthly
      .map(
        (r) => `<tr>
        <td>${r.month}</td>
        <td>${r.new_customers}</td>
        <td>${r.cumulative_unique_customers}</td>
        <td>${r.ltm_unique_customers}</td>
        <td>${r.ltm_repeat_customers}</td>
        <td>${r.repeat_rate_pct}%</td>
      </tr>`
      )
      .join("");

    const cumCanvas = document.getElementById("chart-cumulative");
    const repCanvas = document.getElementById("chart-repeat");
    drawLineChart(
      cumCanvas,
      monthly.map((r) => ({
        x: r.month,
        y: r.cumulative_unique_customers,
      })),
      {
        color: "#c4a35a",
        fill: "rgba(196, 163, 90, 0.12)",
        min: 0,
      }
    );
    drawLineChart(
      repCanvas,
      monthly.map((r) => ({ x: r.month, y: r.repeat_rate_pct })),
      {
        color: "#7cb89a",
        fill: "rgba(124, 184, 154, 0.12)",
        min: 0,
        max: 100,
        formatY: (v) => `${Math.round(v)}%`,
      }
    );
  } catch (_) {
    rangeEl.textContent = "Could not load KPIs";
  }
}

renderSelectedFiles();
refreshUploadLog();
loadCustomerKpis();
window.addEventListener("resize", () => {
  // Redraw charts at the new width without another fetch
  const tbody = document.querySelector("#kpi-table tbody");
  if (tbody && tbody.children.length) loadCustomerKpis();
});
