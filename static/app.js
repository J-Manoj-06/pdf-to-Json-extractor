const form = document.querySelector("#uploadForm");
const output = document.querySelector("#output");
const downloadLink = document.querySelector("#downloadLink");
const button = form.querySelector("button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  button.disabled = true;
  button.textContent = "Extracting...";
  output.textContent = "Reading PDF and building structure...";
  downloadLink.hidden = true;

  try {
    const response = await fetch("/api/extract", {
      method: "POST",
      body: new FormData(form),
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Extraction failed.");
    }

    output.textContent = JSON.stringify(payload.book, null, 2);
    downloadLink.href = payload.jsonUrl;
    downloadLink.hidden = false;
  } catch (error) {
    output.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Extract JSON";
  }
});
