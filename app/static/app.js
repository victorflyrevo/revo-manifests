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

renderSelectedFiles();
refreshUploadLog();
