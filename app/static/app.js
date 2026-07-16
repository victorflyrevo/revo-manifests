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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!input.files?.length) return;

  results.hidden = false;
  results.innerHTML = "";
  submit.disabled = true;
  submit.textContent = "Processing…";

  for (const file of input.files) {
    const box = document.createElement("div");
    box.className = "result";
    box.textContent = `Uploading ${file.name}…`;
    results.appendChild(box);

    try {
      const body = new FormData();
      body.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);

      box.classList.add("ok");
      box.innerHTML = `<strong>${data.filename}</strong><br/>
        found ${data.flights_found} flights ·
        inserted <code>${data.flights_inserted}</code> ·
        skipped ${data.flights_skipped} ·
        boardings <code>${data.boardings_inserted}</code>
        ${data.notes ? `<br/><span class="muted">${data.notes}</span>` : ""}`;
    } catch (err) {
      box.classList.add("err");
      box.textContent = `${file.name}: ${err.message || err}`;
    }
  }

  submit.textContent = "Process uploads";
  refreshSubmit();
  setTimeout(() => location.reload(), 900);
});
