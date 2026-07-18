document.querySelectorAll(".guide-copy").forEach((button) => {
  const original = button.textContent;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select + copy";
    }
    window.setTimeout(() => {
      button.textContent = original;
    }, 1600);
  });
});
