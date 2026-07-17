const form = document.getElementById("upload-form");
const input = document.getElementById("file");
const drop = document.getElementById("drop");
const submit = document.getElementById("submit");
const results = document.getElementById("results");

function refreshSubmit() {
  submit.disabled = !input.files || input.files.length === 0;
}

input.addEventListener("change", refreshSubmit);

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
  input.files = e.dataTransfer.files;
  refreshSubmit();
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
  if (!input.files?.length) return;

  // Copy FileList — process every selected file, no client-side cap
  const files = Array.from(input.files);
  const total = files.length;

  results.hidden = false;
  results.innerHTML = "";
  submit.disabled = true;

  let ok = 0;
  let fail = 0;

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    submit.textContent = `Processing ${i + 1} / ${total}…`;

    const box = document.createElement("div");
    box.className = "result";
    box.textContent = `(${i + 1}/${total}) Uploading ${file.name}…`;
    results.appendChild(box);
    box.scrollIntoView({ block: "nearest" });

    try {
      const body = new FormData();
      body.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        /* ignore */
      }
      if (!res.ok) throw new Error(errDetail(data, res));

      ok += 1;
      box.classList.add("ok");
      box.innerHTML = `<strong>(${i + 1}/${total}) ${data.filename}</strong><br/>
        sheets/flights found <code>${data.flights_found}</code> ·
        inserted <code>${data.flights_inserted}</code> ·
        skipped ${data.flights_skipped} ·
        boardings <code>${data.boardings_inserted}</code>
        ${data.notes ? `<br/><span class="muted">${data.notes}</span>` : ""}`;
    } catch (err) {
      fail += 1;
      box.classList.add("err");
      box.textContent = `(${i + 1}/${total}) ${file.name}: ${err.message || err}`;
      // continue with remaining files — never abort the batch
    }
  }

  submit.textContent = `Done — ${ok} ok, ${fail} failed of ${total}`;
  refreshSubmit();

  // Reload only after the full batch finishes
  setTimeout(() => location.reload(), 1500);
});
